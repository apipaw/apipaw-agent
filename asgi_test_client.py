"""
asgi_test_client.py —— **零依赖**的 ASGI 测试客户端（阶段十六 P0）

背景
----
仓库里三个用例（阶段七 P2 / 阶段十三 P2 / 阶段十五 P2）原本用
``starlette.testclient.TestClient``。但 **starlette ≥ 1.x 的 TestClient 依赖
``httpx``**，而 ``httpx`` 在发行依赖里是**可选**项（``requirements.txt`` 里
被注释掉）⇒ 一台只按 ``requirements.txt`` 装依赖的机器上，这三个用例会
在 ``from starlette.testclient import TestClient`` 这一行直接
``RuntimeError: The starlette.testclient module requires the httpx package``。

那不是产品缺陷，是**测试基础设施的隐式依赖** —— 于是"全量单测"永远无法
100% 绿灯。

为什么自己写而不是把 httpx 变成硬依赖
------------------------------------
- **发行包红线**：本仓库的发行包**零新增运行时依赖**，把 ``httpx`` 提到
  硬依赖会让每个节点都多装一个用不到的包；
- **确定性**：只用标准库 ⇒ 任何机器、任何 venv 都能直接跑；
- **够用即可**：这三个用例只用到 ``get()`` / ``post(json=...)`` 与
  ``status_code`` / ``json()`` / ``text``，没必要背一整套 HTTP 客户端。

用法::

    from asgi_test_client import TestClient
    client = TestClient(app)
    resp = client.post("/api/ollama/pull", json={"model": "qwen2.5:0.5b"})
    print(resp.status_code, resp.json())

⚠️ **不进 lifespan**：``TestClient(app)`` 只发 ``http`` 作用域的请求，
不会触发 FastAPI 的 startup/shutdown ⇒ 不会启动 WS 隧道与遥测
（否则用例会真的连上生产云端）。
"""

from __future__ import annotations

import asyncio
import json as _json
from typing import Any, Dict, List, Optional, Tuple
from urllib.parse import urlsplit


class ASGIResponse(object):
    """一次 ASGI 请求的响应（够用即可）。"""

    def __init__(self, status_code: int, headers: List[Tuple[bytes, bytes]],
                 content: bytes) -> None:
        self.status_code = int(status_code)
        self._headers = headers
        self.content = bytes(content or b"")

    @property
    def headers(self) -> Dict[str, str]:
        """响应头字典（**键统一小写**，取值时大小写不敏感）。"""
        out: Dict[str, str] = {}
        for raw_key, raw_value in self._headers:
            try:
                key = raw_key.decode("latin-1")
                value = raw_value.decode("latin-1")
            except Exception:  # noqa: BLE001 - 头解析失败不影响主体结论
                continue
            out[key.lower()] = value
        return out

    @property
    def text(self) -> str:
        return self.content.decode("utf-8", "replace")

    def json(self) -> Any:
        raw = self.text
        if not raw.strip():
            return None
        return _json.loads(raw)

    def __repr__(self) -> str:  # pragma: no cover - 调试辅助
        return "<ASGIResponse %d %r>" % (self.status_code, self.text[:200])


class TestClient(object):
    """极简 ASGI 客户端（与 ``starlette.testclient.TestClient`` 兼容的那一小片 API）。

    - ``get(path)`` / ``post(path, json=...)``
    - 响应对象提供 ``status_code`` / ``json()`` / ``text`` / ``content`` / ``headers``
    """

    def __init__(self, app: Any, base_url: str = "http://testserver") -> None:
        self.app = app
        self.base_url = base_url.rstrip("/")

    # ── 请求 ────────────────────────────────────────────────
    def request(self, method: str, path: str,
                json: Optional[Any] = None,
                headers: Optional[Dict[str, str]] = None,
                params: Optional[Dict[str, str]] = None) -> ASGIResponse:
        parts = urlsplit(path)
        query = parts.query
        if params:
            extra = "&".join("%s=%s" % (k, v) for k, v in params.items())
            query = ("%s&%s" % (query, extra)) if query else extra

        body = b""
        raw_headers: Dict[str, str] = {"host": "testserver"}
        if json is not None:
            body = _json.dumps(json, ensure_ascii=False).encode("utf-8")
            raw_headers["content-type"] = "application/json"
            raw_headers["content-length"] = str(len(body))
        if headers:
            for key, value in headers.items():
                raw_headers[str(key).lower()] = str(value)

        scope: Dict[str, Any] = {
            "type": "http",
            "asgi": {"version": "3.0", "spec_version": "2.3"},
            "http_version": "1.1",
            "method": method.upper(),
            "path": parts.path or "/",
            "raw_path": (parts.path or "/").encode("utf-8"),
            "query_string": query.encode("utf-8"),
            "root_path": "",
            "scheme": "http",
            "server": ("testserver", 80),
            "client": ("testclient", 50000),
            "headers": [(k.encode("latin-1"), v.encode("latin-1"))
                        for k, v in raw_headers.items()],
        }
        return self._run(scope, body)

    def get(self, path: str, **kwargs: Any) -> ASGIResponse:
        return self.request("GET", path, **kwargs)

    def post(self, path: str, **kwargs: Any) -> ASGIResponse:
        return self.request("POST", path, **kwargs)

    def put(self, path: str, **kwargs: Any) -> ASGIResponse:
        return self.request("PUT", path, **kwargs)

    def delete(self, path: str, **kwargs: Any) -> ASGIResponse:
        return self.request("DELETE", path, **kwargs)

    # ── ASGI 驱动 ───────────────────────────────────────────
    def _run(self, scope: Dict[str, Any], body: bytes) -> ASGIResponse:
        state: Dict[str, Any] = {"status": 500, "headers": [], "chunks": []}
        delivered = {"request": False}

        async def receive() -> Dict[str, Any]:
            if not delivered["request"]:
                delivered["request"] = True
                return {"type": "http.request", "body": body, "more_body": False}
            return {"type": "http.disconnect"}

        async def send(message: Dict[str, Any]) -> None:
            kind = message.get("type")
            if kind == "http.response.start":
                state["status"] = int(message.get("status") or 500)
                state["headers"] = list(message.get("headers") or [])
            elif kind == "http.response.body":
                state["chunks"].append(message.get("body") or b"")

        async def _call() -> None:
            await self.app(scope, receive, send)

        asyncio.run(_call())
        return ASGIResponse(state["status"], state["headers"], b"".join(state["chunks"]))


__all__ = ["ASGIResponse", "TestClient"]
