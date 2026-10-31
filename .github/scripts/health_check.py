"""
health_check.py —— CI 健康检查：启动本地服务并校验 OpenAI 规范接口

被 `.github/workflows/ci.yml` 调用，也可在本地手动执行：
    python .github/scripts/health_check.py

检查项：
1. ``GET  /health``                -> 200，且包含状态 / GPU / 让退指标
2. ``GET  /v1/models``             -> 200，且为标准 OpenAI 列表结构
3. ``POST /v1/chat/completions``   -> 200（APIPAW_FORCE_IDLE=1 下节点恒为 IDLE）
4. ``POST /v1/chat/completions``   -> 流式 SSE，首块为 ``data: `` 且以 ``[DONE]`` 收尾
"""

from __future__ import annotations

import json
import os
import sys
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT))

# 联调/CI 环境下强制保持 IDLE，避免宿主环境的键鼠活动让节点进入 YIELDED
os.environ.setdefault("APIPAW_FORCE_IDLE", "1")
# CI 绝不允许向云端真实上报心跳
os.environ["APIPAW_DISABLE_TELEMETRY"] = "1"

HOST = "127.0.0.1"
PORT = int(os.getenv("APIPAW_CI_PORT", "52455"))
BASE = f"http://{HOST}:{PORT}"


def _get(path: str, timeout: float = 10.0) -> tuple[int, bytes]:
    req = urllib.request.Request(BASE + path, method="GET")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.status, resp.read()
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read()


def _post_json(path: str, payload: dict, timeout: float = 20.0) -> tuple[int, bytes]:
    data = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(
        BASE + path,
        data=data,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.status, resp.read()
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read()


def _wait_ready(timeout: float = 30.0) -> None:
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            status, _ = _get("/health", timeout=2.0)
            if status == 200:
                return
        except Exception:
            pass
        time.sleep(0.3)
    raise TimeoutError(f"服务在 {timeout}s 内未就绪: {BASE}/health")


def main() -> int:
    import uvicorn

    from main import create_app

    app = create_app()
    server = uvicorn.Server(
        uvicorn.Config(app, host=HOST, port=PORT, log_level="warning", access_log=False)
    )
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()

    try:
        _wait_ready()

        # 1) /health
        status, body = _get("/health")
        assert status == 200, f"/health 期望 200，实际 {status}"
        health = json.loads(body)
        for key in ("status", "state", "gpu", "yield_metrics", "node_id"):
            assert key in health, f"/health 缺少字段 {key}"
        assert health["status"] == "ok", f"/health status 非 ok: {health['status']}"
        print(f"[OK] /health 200 | state={health['state']} | gpu={health['gpu']}")

        # 2) /v1/models
        status, body = _get("/v1/models")
        assert status == 200, f"/v1/models 期望 200，实际 {status}"
        models = json.loads(body)
        assert models.get("object") == "list", "/v1/models 返回结构非 OpenAI 规范"
        assert models.get("data"), "/v1/models 列表为空"
        print(f"[OK] /v1/models 200 | {len(models['data'])} 个模型")

        # 3) /v1/chat/completions (非流式)
        status, body = _post_json(
            "/v1/chat/completions",
            {
                "model": "apipaw-gpt2-small",
                "messages": [{"role": "user", "content": "health check"}],
                "stream": False,
            },
        )
        assert status == 200, f"/v1/chat/completions 期望 200，实际 {status}: {body[:200]!r}"
        completion = json.loads(body)
        assert completion.get("choices"), "chat/completions 缺少 choices"
        assert completion["choices"][0]["message"]["content"], "chat/completions 内容为空"
        assert completion.get("usage"), "chat/completions 缺少 usage"
        print("[OK] /v1/chat/completions 200 (non-stream)")

        # 4) /v1/chat/completions (SSE 流式)
        req = urllib.request.Request(
            BASE + "/v1/chat/completions",
            data=json.dumps(
                {
                    "model": "apipaw-gpt2-small",
                    "messages": [{"role": "user", "content": "health check"}],
                    "stream": True,
                }
            ).encode("utf-8"),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=20.0) as resp:
            assert resp.status == 200, f"流式请求非 200: {resp.status}"
            raw = resp.read().decode("utf-8", errors="replace")
        assert "data: " in raw, "流式响应缺少 data: 分片"
        assert raw.rstrip().endswith("[DONE]"), "流式响应未以 [DONE] 收尾"
        print("[OK] /v1/chat/completions 200 (SSE stream, [DONE] 收尾)")

        print("\nALL HEALTH CHECKS PASSED")
        return 0
    finally:
        server.should_exit = True
        thread.join(timeout=10)


if __name__ == "__main__":
    sys.exit(main())
