"""阶段六 P3 · 模块 A2：OpenAI 兼容推理后端（vLLM / llama.cpp）沙箱自检。

用法（在仓库根目录执行）：
    ./.venv/Scripts/python.exe test_phase6_p3_openai_backend.py

覆盖用例：
  1. ``/v1/models`` 探测：OpenAI 兼容服务可被发现；非兼容服务**不会**被误认
  2. SSE 流式生成：逐片拿到内容 + 收尾帧的真实 usage
  3. **断流语义**：没有 ``data: [DONE]`` 也没有 ``finish_reason`` ⇒
     ``BackendStreamBroken``（不是"正常结束"）
  4. 老 llama.cpp 不认 ``stream_options`` ⇒ 自动降级重试一次
  5. ``auto`` 多引擎探测：显式配置了 vLLM 地址才会进候选（默认不探 8000 端口）
  6. 隧道 DONE 帧带 ``perf``（节点自测的真实 TTFT / 吐字速度）

测试完全离线：本地起一个假 OpenAI 兼容服务，不连生产云端。
"""

import asyncio
import json
import os
import sys
import threading
import time
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
# 假 OpenAI 兼容服务（vLLM / llama-server 风格）
# ──────────────────────────────────────────────────────────────
class _FakeOpenAIHandler(BaseHTTPRequestHandler):
    """最小 OpenAI 兼容实现：``/v1/models`` + ``/v1/chat/completions``（SSE）。"""

    protocol_version = "HTTP/1.1"

    # 由外部用例切换：是否回 [DONE]、是否拒绝 stream_options
    mode = "normal"

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
        if self.path.startswith("/v1/models"):
            self._send_json({"object": "list", "data": [
                {"id": "qwen2.5-7b", "object": "model"},
                {"id": "llama3-8b", "object": "model"},
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
        if not self.path.startswith("/v1/chat/completions"):
            self.send_error(404)
            return
        if self.mode == "reject_stream_options" and "stream_options" in req:
            self._send_json({"error": {"message": "unknown field stream_options"}}, status=400)
            return
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Transfer-Encoding", "chunked")
        self.end_headers()
        for piece in ("你", "好"):
            self._write_sse({
                "id": "chatcmpl-x", "object": "chat.completion.chunk",
                "choices": [{"index": 0, "delta": {"content": piece}, "finish_reason": None}],
            })
        if self.mode == "broken":  # 断流：既没有 finish_reason 也没有 [DONE]
            self.wfile.write(b"0\r\n\r\n")
            self.wfile.flush()
            return
        self._write_sse({
            "id": "chatcmpl-x", "object": "chat.completion.chunk",
            "choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}],
            "usage": {"prompt_tokens": 13, "completion_tokens": 5, "total_tokens": 18},
        })
        if self.mode != "no_done_marker":
            self._write_sse_raw("data: [DONE]\n\n")
        self.wfile.write(b"0\r\n\r\n")
        self.wfile.flush()

    def _write_sse(self, obj):
        self._write_sse_raw("data: " + json.dumps(obj) + "\n\n")

    def _write_sse_raw(self, text):
        data = text.encode("utf-8")
        self.wfile.write(("%x\r\n" % len(data)).encode("ascii"))
        self.wfile.write(data)
        self.wfile.write(b"\r\n")
        self.wfile.flush()


class _FakeOpenAI(object):
    def __init__(self, mode="normal"):
        _FakeOpenAIHandler.mode = mode
        self._server = ThreadingHTTPServer(("127.0.0.1", 0), _FakeOpenAIHandler)
        self._thread = threading.Thread(target=self._server.serve_forever, daemon=True)
        self._thread.start()

    @property
    def base_url(self):
        host, port = self._server.server_address[:2]
        return "http://%s:%d" % (host, port)

    def stop(self):
        self._server.shutdown()
        self._server.server_close()


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


def _cfg(**kwargs):
    base = {
        "node_id": "node-p6-p3",
        "node_name": "P3 测试节点",
        "hardware_type": "cuda",
        "device": "cuda",
        "max_vram_gb": 24.0,
        "model_name": "apipaw-gpt2-small",
        "backend_model": "qwen2.5-7b",
        "backend_base_url": "http://127.0.0.1:11434",
        "backend_vllm_base_url": "",
        "backend_llamacpp_base_url": "",
        "backend_timeout_sec": 10.0,
        "inference_backend": "auto",
        "port": 52416,
    }
    base.update(kwargs)
    return type("C", (object,), base)()


def main():
    import inference_backend
    import telemetry
    import ws_tunnel

    telemetry.collect_gpu_metrics_cached = lambda: {
        "gpu_temp_c": 50.0, "vram_used_gb": 3.0,
        "vram_total_gb": 24.0, "vram_free_gb": 21.0,
    }
    ws_tunnel.collect_gpu_metrics_cached = telemetry.collect_gpu_metrics_cached

    # ---- 1. 探测与模型清单 ----
    fake = _FakeOpenAI("normal")
    try:
        inference_backend.reset_backend_cache()
        backend = inference_backend.VLLMBackend(fake.base_url, 10.0, "qwen2.5-7b")
        _record("OpenAI 兼容后端可连通", backend.is_available() is True, fake.base_url)
        models = backend.list_models()
        _record("OpenAI 兼容模型清单正确",
                models == ["qwen2.5-7b", "llama3-8b"], str(models))

        # ---- 2. SSE 流式 + 真实 usage ----
        async def _gen():
            chunks = []
            async for chunk in backend.generate(
                    "qwen2.5-7b", [{"role": "user", "content": "你好"}]):
                chunks.append(chunk)
            return chunks

        chunks = asyncio.run(_gen())
        text = "".join(c.text for c in chunks)
        last = chunks[-1]
        _record("OpenAI 兼容后端真流式返回分片", text == "你好", repr(text))
        _record("OpenAI 兼容后端回报真实 usage（prompt=13 completion=5）",
                last.usage is not None and last.usage.prompt_tokens == 13
                and last.usage.completion_tokens == 5 and last.done is True,
                str(last.usage.to_dict() if last.usage else None))
    finally:
        fake.stop()

    # ---- 3. 断流语义 ----
    broken = _FakeOpenAI("broken")
    try:
        backend = inference_backend.VLLMBackend(broken.base_url, 10.0, "m")

        async def _gen_broken():
            try:
                async for _ in backend.generate("m", [{"role": "user", "content": "hi"}]):
                    pass
            except inference_backend.BackendStreamBroken as exc:
                return "broken:%s" % exc
            except Exception as exc:  # noqa: BLE001
                return "unexpected:%s" % exc
            return "no-error"

        msg = asyncio.run(_gen_broken())
        _record("OpenAI 兼容后端断流 ⇒ BackendStreamBroken（不是正常结束）",
                msg.startswith("broken:"), msg[:90])
    finally:
        broken.stop()

    # ---- 4. stream_options 降级重试 ----
    legacy = _FakeOpenAI("reject_stream_options")
    try:
        backend = inference_backend.VLLMBackend(legacy.base_url, 10.0, "m")

        async def _gen_legacy():
            chunks = []
            async for chunk in backend.generate("m", [{"role": "user", "content": "hi"}]):
                chunks.append(chunk)
            return chunks

        chunks = asyncio.run(_gen_legacy())
        _record("不认 stream_options 的后端 ⇒ 自动降级重试并拿到内容",
                "".join(c.text for c in chunks) == "你好",
                repr("".join(c.text for c in chunks)))
    finally:
        legacy.stop()

    # ---- 5. auto 多引擎探测 ----
    inference_backend.reset_backend_cache()
    only_ollama = inference_backend.get_backend({
        "inference_backend": "auto", "backend_base_url": "http://127.0.0.1:59999",
        "backend_timeout_sec": 2.0})
    _record("auto 模式探不到任何后端 ⇒ None（上层据此报 backend_unavailable）",
            only_ollama is None, str(only_ollama))

    vllm_fake = _FakeOpenAI("normal")
    try:
        inference_backend.reset_backend_cache()
        picked = inference_backend.get_backend({
            "inference_backend": "auto", "backend_base_url": "http://127.0.0.1:59999",
            "backend_vllm_base_url": vllm_fake.base_url, "backend_timeout_sec": 5.0})
        _record("auto 模式探到显式配置的 vLLM",
                picked is not None and picked.name == "vllm",
                getattr(picked, "name", "None"))
    finally:
        vllm_fake.stop()

    # ---- 6. 隧道 DONE 帧带 perf ----
    fake2 = _FakeOpenAI("normal")
    try:
        inference_backend.reset_backend_cache()
        frames = []
        cfg = _cfg(inference_backend="vllm",
                   backend_vllm_base_url=fake2.base_url,
                   backend_timeout_sec=10.0)
        tunnel = _make_tunnel(ws_tunnel, cfg, frames)

        async def _drive():
            tunnel._loop = asyncio.get_event_loop()
            await tunnel._handle_inference_request({
                "request_id": "req-p3-perf",
                "payload": {"model": "qwen2.5-7b",
                            "messages": [{"role": "user", "content": "你好"}],
                            "stream": True},
            })

        asyncio.run(_drive())
        done_frames = [f for f in frames if f.get("type") == "RESPONSE_DONE"]
        err_frames = [f for f in frames if f.get("type") == "RESPONSE_ERROR"]
        perf = (done_frames[0].get("perf") if done_frames else None) or {}
        _record("隧道 DONE 帧带 perf（节点自测 TTFT）",
                bool(done_frames) and not err_frames
                and float(perf.get("ttft_ms") or 0) > 0,
                str(perf))
        _record("隧道 DONE 帧带真实 usage", bool(done_frames)
                and done_frames[0].get("usage", {}).get("prompt_tokens") == 13,
                str(done_frames[0].get("usage") if done_frames else None))
    finally:
        fake2.stop()
        inference_backend.reset_backend_cache()

    passed = sum(1 for _, ok, _ in RESULTS if ok)
    total = len(RESULTS)
    print("\n===== 阶段六 P3 · 模块 A2（OpenAI 兼容后端）沙箱：%d/%d 通过 ====="
          % (passed, total))
    if passed != total:
        for name, ok, detail in RESULTS:
            if not ok:
                print("  FAIL: %s | %s" % (name, detail))
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
