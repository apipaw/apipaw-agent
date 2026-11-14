"""阶段六 P2 · A4：断流错误语义自检（Agent 侧）。

用法（在仓库根目录执行）：
    ./.venv/Scripts/python.exe test_phase6_p2_stream_broken.py

覆盖用例：
  1. 推理**跑到一半**后端挂掉 ⇒ 隧道回 ``RESPONSE_ERROR{error:"backend_stream_broken"}``，
     且此前已经发过分片（云端据此判定「断流 ⇒ 按 D6 不计费」）。
  2. 开跑前就挂（压根连不上）⇒ 仍是 ``backend_unavailable``（**不能**混成断流）。
  3. **不做静默重试**：同一个 request_id 只回**一次**错误帧，绝不重来一遍。

测试完全离线：本地起一个"吐一半就死"的假 Ollama 服务，不连生产云端。
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
    RESULTS.append((name, bool(ok), detail))
    print(("[PASS] " if ok else "[FAIL] ") + name + (" | " + detail if detail else ""))


# ──────────────────────────────────────────────────────────────
# 假 Ollama：吐一个分片就**掐断连接**（模拟推理中途进程挂掉）
# ──────────────────────────────────────────────────────────────
class _HalfDeadOllamaHandler(BaseHTTPRequestHandler):
    """``/api/tags`` 正常；``/api/chat`` 吐一个分片后直接断开 TCP。"""

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
            self._send_json({"models": [{"name": "qwen2.5:7b", "size": 1}]})
            return
        self.send_error(404)

    def do_POST(self):
        length = int(self.headers.get("Content-Length") or 0)
        if length:
            self.rfile.read(length)
        if not self.path.startswith("/api/chat"):
            self.send_error(404)
            return
        self.send_response(200)
        self.send_header("Content-Type", "application/x-ndjson")
        self.send_header("Transfer-Encoding", "chunked")
        self.end_headers()
        data = (json.dumps({
            "message": {"role": "assistant", "content": "半"}, "done": False,
        }) + "\n").encode("utf-8")
        self.wfile.write(("%x\r\n" % len(data)).encode("ascii"))
        self.wfile.write(data)
        self.wfile.write(b"\r\n")
        self.wfile.flush()
        # ⚠️ 关键：**不给**结尾的 0\r\n\r\n，直接挂连接 ⇒ 客户端读到一半就没了
        try:
            self.connection.close()
        except Exception:  # noqa: BLE001
            pass
        self.close_connection = True


class _HalfDeadOllama(object):
    def __init__(self):
        self._server = ThreadingHTTPServer(("127.0.0.1", 0), _HalfDeadOllamaHandler)
        self._thread = threading.Thread(target=self._server.serve_forever, daemon=True)
        self._thread.start()

    @property
    def base_url(self):
        host, port = self._server.server_address[:2]
        return "http://%s:%d" % (host, port)

    def stop(self):
        self._server.shutdown()
        self._server.server_close()


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
        "node_id": "node-p6-p2-test",
        "node_name": "P2 测试节点",
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


async def _drive(tunnel, msg):
    tunnel._loop = asyncio.get_event_loop()
    await tunnel._handle_inference_request(msg)


def _free_port():
    import socket
    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
    sock.close()
    return port


def main():
    import inference_backend
    import telemetry
    import ws_tunnel

    telemetry.collect_gpu_metrics_cached = lambda: {
        "gpu_temp_c": 50.0, "vram_used_gb": 3.0,
        "vram_total_gb": 24.0, "vram_free_gb": 21.0,
    }

    fake = _HalfDeadOllama()
    try:
        # ---- 1. 跑到一半挂掉 ⇒ backend_stream_broken ----
        inference_backend.reset_backend_cache()
        frames = []
        cfg = _cfg(inference_backend="auto", backend_base_url=fake.base_url,
                   backend_timeout_sec=10.0)
        tunnel = _make_tunnel(ws_tunnel, cfg, frames)
        asyncio.run(_drive(tunnel, {
            "request_id": "req-broken-1",
            "payload": {"model": "qwen2.5:7b",
                        "messages": [{"role": "user", "content": "你好"}],
                        "stream": True},
        }))
        chunk_frames = [f for f in frames if f.get("type") == "RESPONSE_CHUNK"]
        err_frames = [f for f in frames if f.get("type") == "RESPONSE_ERROR"]
        done_frames = [f for f in frames if f.get("type") == "RESPONSE_DONE"]

        _record("断流前已发出分片（云端据此判定不计费）",
                len(chunk_frames) >= 1, "chunks=%d" % len(chunk_frames))
        _record("断流 ⇒ 回 RESPONSE_ERROR{backend_stream_broken}",
                bool(err_frames) and err_frames[0].get("error") == "backend_stream_broken",
                str(err_frames[:1]))
        _record("断流 ⇒ **不**回 RESPONSE_DONE（没有完整结果）",
                not done_frames, "done=%s" % done_frames[:1])
        _record("断流 ⇒ 只回**一次**错误帧（不做静默重试）",
                len(err_frames) == 1, "err_count=%d" % len(err_frames))

        # ---- 2. 开跑前就挂 ⇒ 仍是 backend_unavailable（不能混成断流）----
        inference_backend.reset_backend_cache()
        frames = []
        cfg = _cfg(inference_backend="auto",
                   backend_base_url="http://127.0.0.1:%d" % _free_port(),
                   backend_timeout_sec=2.0, port=_free_port())
        tunnel = _make_tunnel(ws_tunnel, cfg, frames)
        asyncio.run(_drive(tunnel, {
            "request_id": "req-none-1",
            "payload": {"model": "qwen2.5:7b",
                        "messages": [{"role": "user", "content": "你好"}],
                        "stream": True},
        }))
        err_frames = [f for f in frames if f.get("type") == "RESPONSE_ERROR"]
        chunk_frames = [f for f in frames if f.get("type") == "RESPONSE_CHUNK"]
        _record("开跑前失败 ⇒ backend_unavailable（不是断流）",
                bool(err_frames) and err_frames[0].get("error") == "backend_unavailable",
                str(err_frames[:1]))
        _record("开跑前失败 ⇒ 不回任何分片",
                not chunk_frames, "chunks=%d" % len(chunk_frames))
    finally:
        fake.stop()

    passed = sum(1 for _, ok, _ in RESULTS if ok)
    total = len(RESULTS)
    print("\n===== 阶段六 P2 · A4 断流语义自检：%d/%d 通过 =====" % (passed, total))
    return 0 if passed == total else 1


if __name__ == "__main__":
    sys.exit(main())
