"""
test_health_fast.py —— /health 快速响应回归自检（阶段六 P4 · CI 修复）

背景
----
阶段六 P3 引入推理后端抽象层后，``/health`` 变成这样：

    async def health():                       # ← async
        inference_backend.describe_backend(cfg)  # ← 阻塞的 HTTP 探测

两件事叠在一起就是**致命组合**：

1. ``async def`` 端点里做**阻塞** I/O ⇒ 整个事件循环被堵死，
   节点本地服务对所有请求（含 ``/v1/chat/completions``）都无响应；
2. 探测用的是 ``backend_timeout_sec``（默认 60s）⇒ 一次慢探测能卡 60 秒。

表现为 CI 的 ``Health API Verification`` 步骤恒定失败：
``TimeoutError: 服务在 30.0s 内未就绪``（就绪轮询每次只给 2s 预算）。

本文件锁死三条不变量：

- **I1** ``/health`` 与 ``/v1/models`` 必须是**同步**端点
  （FastAPI 会把它们丢进线程池，阻塞调用不再伤害事件循环）；
- **I2** 健康检查的探测必须有界（``HEALTH_PROBE_TIMEOUT_SEC``），
  后端"卡死"时 ``/health`` 仍须在数秒内返回；
- **I3** 健康检查**不得污染**共享后端缓存 —— 否则一次探测超时会把
  "本机没后端"这个结论钉死 30 秒，真正开始推理时也要干等。
"""

from __future__ import annotations

import asyncio
import inspect
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Tuple

REPO_ROOT = Path(__file__).resolve().parents[0]
sys.path.insert(0, str(REPO_ROOT))

import inference_backend  # noqa: E402

_PASS = 0
_FAIL = 0


def _record(name: str, ok: bool, detail: str = "") -> None:
    global _PASS, _FAIL
    if ok:
        _PASS += 1
        print("[PASS] %s | %s" % (name, detail))
    else:
        _FAIL += 1
        print("[FAIL] %s | %s" % (name, detail))


class _SlowHandler(BaseHTTPRequestHandler):
    """一个"卡死"的推理引擎：任何请求都睡 30 秒才回应。"""

    delay_sec = 30.0

    def do_GET(self):  # noqa: N802 - BaseHTTPRequestHandler 约定
        time.sleep(self.delay_sec)
        try:
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(b'{"data": []}')
        except OSError:
            pass

    def log_message(self, *args):  # noqa: D401 - 静默
        pass


def _start_slow_server() -> Tuple[str, ThreadingHTTPServer]:
    server = ThreadingHTTPServer(("127.0.0.1", 0), _SlowHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    return "http://127.0.0.1:%d" % server.server_address[1], server


def check_sync_endpoints() -> None:
    """I1：``/health`` / ``/v1/models`` 必须是同步端点。"""
    from main import create_app

    app = create_app()
    endpoints = {}
    for route in app.routes:
        path = getattr(route, "path", "")
        endpoint = getattr(route, "endpoint", None)
        if path in ("/health", "/v1/models") and endpoint is not None:
            endpoints[path] = endpoint

    for path in ("/health", "/v1/models"):
        endpoint = endpoints.get(path)
        _record(
            "%s 路由存在" % path,
            endpoint is not None,
            str(endpoint),
        )
        if endpoint is None:
            continue
        _record(
            "%s 是同步端点（阻塞探测不堵事件循环）" % path,
            not asyncio.iscoroutinefunction(endpoint),
            "iscoroutinefunction=%s" % asyncio.iscoroutinefunction(endpoint),
        )


def check_bounded_probe() -> None:
    """I2：后端卡死时，健康检查仍须有界返回。"""
    url, server = _start_slow_server()
    cfg = {
        "inference_backend": "ollama",
        "backend_base_url": url,
        "backend_timeout_sec": 30.0,
    }
    inference_backend.reset_backend_cache()
    started = time.time()
    snapshot = inference_backend.describe_backend(cfg, use_cache=False)
    elapsed = time.time() - started
    _record(
        "后端卡死时 /health 有界返回",
        elapsed < 5.0,
        "耗时 %.2fs（预算 %.1fs）" % (elapsed, inference_backend.HEALTH_PROBE_TIMEOUT_SEC),
    )
    _record(
        "探不到后端 ⇒ available=False（不是异常）",
        snapshot.get("available") is False,
        str(snapshot),
    )

    # 快照缓存：第二次应当近乎瞬时
    started = time.time()
    inference_backend.describe_backend(cfg)
    cached_elapsed = time.time() - started
    _record(
        "健康快照走 TTL 缓存",
        cached_elapsed < 0.5,
        "命中缓存耗时 %.3fs" % cached_elapsed,
    )
    server.shutdown()
    server.server_close()


def check_no_cache_poisoning() -> None:
    """I3：健康检查不得把"探不到"写进共享后端缓存。"""
    url, server = _start_slow_server()
    cfg = {
        "inference_backend": "ollama",
        "backend_base_url": url,
        "backend_timeout_sec": 30.0,
    }
    inference_backend.reset_backend_cache()
    inference_backend.describe_backend(cfg, use_cache=False)
    _record(
        "健康检查不污染共享后端缓存",
        inference_backend.get_cached_backend(cfg) is None,
        "cached=%r" % (inference_backend.get_cached_backend(cfg),),
    )
    server.shutdown()
    server.server_close()


def main() -> int:
    check_sync_endpoints()
    check_bounded_probe()
    check_no_cache_poisoning()
    print("\n===== /health 快速响应回归自检：%d/%d 通过 =====" % (_PASS, _PASS + _FAIL))
    return 0 if _FAIL == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
