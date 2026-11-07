"""
main.py — APIPaw Agent 本地服务入口

对外暴露符合 OpenAI 规范的接口（默认监听 127.0.0.1:52415）：

======================================  ======  ====================================
路由                                     方法    说明
======================================  ======  ====================================
``/health``                              GET     节点健康检查与实时指标
``/v1/models``                           GET     模型列表
``/v1/chat/completions``                 POST    对话补全（支持 stream）
``/ui``                                  GET     本地可视化配置面板（单文件 HTML）
``/api/status``                          GET     面板状态查询（不含明文密钥）
``/api/bind``                            POST    绑定 Node ID / Secret 并热重连
======================================  ======  ====================================

让退语义
--------
用户在电脑上操作时节点处于 ``YIELDED``（已让退）状态，此时接口返回 **503**，
让调用方立即把任务转移到其它节点，而不是让本机抢占用户前台。

线程模型
--------
- Windows 上 ``pystray`` 需要在主线程跑消息循环，因此：
  **有托盘时 uvicorn 跑在后台线程、托盘在主线程**；无托盘时 uvicorn 直接在主线程。
- torch 是可选依赖：未安装时节点依然可以启动并提供 /health 与模拟推理。
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import platform
import sys
import threading
import time
import uuid
import webbrowser
from contextlib import asynccontextmanager
from typing import Any, AsyncIterator, Dict, List, Optional

from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, JSONResponse, StreamingResponse
from pydantic import BaseModel, Field

from agent_config import AGENT_VERSION, get_config
from console import enable_utf8_output
from telemetry import collect_gpu_metrics, get_reporter
from token_counter import build_usage, count_prompt_tokens, count_tokens
from web_ui import UI_HTML
from yield_monitor import NodeState, get_yield_monitor
from ws_tunnel import (
    is_ws_connected,
    reload_tunnel_credentials,
    start_ws_tunnel,
    stop_ws_tunnel,
)

enable_utf8_output()

try:
    import uvicorn
except ImportError:  # pragma: no cover
    uvicorn = None  # type: ignore[assignment]

try:
    from tray_icon import TRAY_AVAILABLE, start_tray, stop_tray
except ImportError:  # pragma: no cover
    TRAY_AVAILABLE = False

    def start_tray():  # type: ignore[misc]
        return None

    def stop_tray() -> None:  # type: ignore[misc]
        return None


# ──────────────────────────────────────────────────────────────
# OpenAI 规范数据模型
# ──────────────────────────────────────────────────────────────
class ChatMessage(BaseModel):
    role: str = Field(default="user")
    content: str = ""


class ChatCompletionRequest(BaseModel):
    model: Optional[str] = None
    messages: List[ChatMessage] = Field(default_factory=list)
    temperature: float = Field(default=1.0, ge=0.0, le=2.0)
    max_tokens: Optional[int] = Field(default=256, ge=1, le=32768)
    top_p: float = Field(default=1.0, ge=0.0, le=1.0)
    stream: bool = False
    user: Optional[str] = None


class BindRequest(BaseModel):
    """本地配置面板的绑定请求（Node ID + Node Secret）。"""

    node_id: str = Field(default="", max_length=128)
    node_secret: str = Field(default="", max_length=512)


# ──────────────────────────────────────────────────────────────
# 应用装配
# ──────────────────────────────────────────────────────────────
@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    """启动/停止后台线程（让退监视器 + WebSocket 主通道 + 心跳兜底）。"""
    cfg = get_config()
    monitor = get_yield_monitor()
    monitor.start()

    print("=" * 50)
    print(f"  APIPaw Agent -- DePIN 算力节点 v{AGENT_VERSION}")
    print("=" * 50)

    reporter = get_reporter()
    if reporter.is_disabled():
        print("[main] 遥测已关闭，不启动 WebSocket 主通道")
    else:
        # WebSocket 长连接是节点的主通信通道：挂机状态与心跳都由隧道承担，
        # HTTP 心跳仅在 WS 掉线期间兜底。
        start_ws_tunnel()
    reporter.start()
    print(
        f"[main] APIPaw Agent {AGENT_VERSION} 已启动｜"
        f"节点 {cfg.node_name}({cfg.node_id})｜"
        f"监听 http://{cfg.host}:{cfg.port}｜"
        f"平台 {platform.system()}"
    )
    # Windows：启动后自动打开本地配置面板（小白用户零配置入口）
    if platform.system() == "Windows":
        threading.Timer(1.5, _open_ui_browser, args=(cfg.host, cfg.port)).start()
    try:
        yield
    finally:
        stop_ws_tunnel()
        monitor.stop()
        reporter.stop()
        stop_tray()
        print("[main] 已退出")


def create_app() -> FastAPI:
    """构建 FastAPI 应用（便于测试中单独实例化）。"""
    cfg = get_config()
    app = FastAPI(
        title="APIPaw Agent",
        version=AGENT_VERSION,
        description="APIPaw 挂机节点本地推理网关（OpenAI 规范接口）",
        lifespan=lifespan,
    )

    # ── /health ──
    @app.get("/health")
    async def health() -> Dict[str, Any]:
        """节点健康检查：返回当前状态、显存、温度与让退指标。"""
        monitor = get_yield_monitor()
        gpu = collect_gpu_metrics()
        return {
            "status": "ok",
            "agent_version": AGENT_VERSION,
            "node_id": cfg.node_id,
            "node_name": cfg.node_name,
            "hardware_type": cfg.hardware_type,
            "state": monitor.state.name,
            "state_label": monitor.state_label,
            "available": monitor.is_idle,
            "model_name": cfg.model_name,
            "api_base_url": cfg.api_base_url,
            "gpu": gpu,
            "yield_metrics": monitor.metrics(),
            "telemetry": get_reporter().status(),
            "platform": platform.system(),
            "timestamp": int(time.time()),
        }

    # ── /v1/models ──
    @app.get("/v1/models")
    async def list_models() -> Dict[str, Any]:
        """模型列表（OpenAI 规范）。"""
        return {
            "object": "list",
            "data": [
                {
                    "id": cfg.model_name,
                    "object": "model",
                    "created": int(time.time()),
                    "owned_by": "apipaw",
                }
            ],
        }

    # ── /v1/chat/completions ──
    @app.post("/v1/chat/completions")
    async def chat_completions(request: ChatCompletionRequest):
        """对话补全。节点已让退/已暂停时返回 503，促使调用方转移任务。"""
        monitor = get_yield_monitor()

        if not monitor.is_idle:
            reason = (
                "节点已让退，用户正在使用电脑"
                if monitor.state == NodeState.YIELDED
                else "节点不可用"
            )
            return JSONResponse(
                status_code=503,
                content={
                    "error": {
                        "message": reason,
                        "type": "node_unavailable",
                        "code": "node_yielded" if monitor.state == NodeState.YIELDED else "node_busy",
                        "param": None,
                    }
                },
            )

        model = request.model or cfg.model_name
        prompt = "\n".join(m.content for m in request.messages if m.content)
        completion_id = f"chatcmpl-{uuid.uuid4().hex[:24]}"
        created = int(time.time())
        # prompt token 按整段 messages 统计（含每条消息的协议开销）
        prompt_tokens = count_prompt_tokens(
            [{"role": m.role, "content": m.content} for m in request.messages]
        )

        if not request.stream:
            content = _simulate_answer(prompt, model)
            return {
                "id": completion_id,
                "object": "chat.completion",
                "created": created,
                "model": model,
                "choices": [
                    {
                        "index": 0,
                        "message": {"role": "assistant", "content": content},
                        "finish_reason": "stop",
                    }
                ],
                "usage": build_usage(prompt_tokens, count_tokens(content)),
            }

        async def event_stream() -> AsyncIterator[str]:
            """SSE 流式输出；逐段检查让退状态，用户一回来立刻中断。"""
            content = _simulate_answer(prompt, model)
            chunks = _chunk_text(content)
            emitted = ""
            for index, piece in enumerate(chunks):
                if not monitor.is_idle:
                    break
                emitted += piece
                payload = {
                    "id": completion_id,
                    "object": "chat.completion.chunk",
                    "created": created,
                    "model": model,
                    "choices": [
                        {
                            "index": 0,
                            "delta": {"content": piece},
                            "finish_reason": None,
                        }
                    ],
                }
                yield f"data: {json.dumps(payload, ensure_ascii=False)}\n\n"
                await asyncio.sleep(0.01)
            final = {
                "id": completion_id,
                "object": "chat.completion.chunk",
                "created": created,
                "model": model,
                "choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}],
                # 真实 token 用量随结束帧一起下发：云端据此做精确计费，
                # 而不是按字符数粗估。
                "usage": build_usage(prompt_tokens, count_tokens(emitted)),
            }
            yield f"data: {json.dumps(final, ensure_ascii=False)}\n\n"
            yield "data: [DONE]\n\n"

        return StreamingResponse(
            event_stream(),
            media_type="text/event-stream",
            headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
        )

    # ── /ui ── 本地可视化配置面板（单文件 HTML，零外部依赖）──
    @app.get("/ui", response_class=HTMLResponse)
    async def ui() -> HTMLResponse:
        """返回本地配置面板页面。"""
        return HTMLResponse(content=UI_HTML)

    # ── /api/status ── 面板状态查询（不回传明文密钥）──
    @app.get("/api/status")
    async def api_status() -> Dict[str, Any]:
        """当前绑定状态与云端连接情况，供面板预填与状态灯使用。"""
        monitor = get_yield_monitor()
        return {
            "ok": True,
            "node_id": cfg.node_id,
            "node_name": cfg.node_name,
            "has_secret": bool(cfg.node_secret),
            "ws_connected": is_ws_connected(),
            "state": monitor.state.name,
            "agent_version": AGENT_VERSION,
            "timestamp": int(time.time()),
        }

    # ── /api/bind ── 绑定凭证：写盘 + 刷新内存 + 隧道平滑重连 ──
    @app.post("/api/bind")
    async def api_bind(req: BindRequest) -> JSONResponse:
        """
        保存 Node ID / Node Secret 并让节点立刻用新凭证连接云端。

        严格按顺序执行：
        1. 校验入参（非空）
        2. 覆写本地 ``agent.config.json`` 的鉴权字段（``AgentConfig.update``
           内部为「原子写盘 + 刷新内存模型」）
        3. 触发 ws_tunnel 平滑重连（断开无效连接 + 唤醒退避 + 重新握手）
        """
        node_id = (req.node_id or "").strip()
        node_secret = (req.node_secret or "").strip()

        # ① 校验
        if not node_id:
            return JSONResponse(
                status_code=400,
                content={"ok": False, "message": "Node ID 不能为空"},
            )
        if not node_secret:
            return JSONResponse(
                status_code=400,
                content={"ok": False, "message": "Node Secret 不能为空"},
            )

        # ② 覆写配置文件 + ③ 刷新内存中的配置单例
        try:
            cfg.update(node_id=node_id, node_secret=node_secret)
        except Exception as exc:  # noqa: BLE001 - 写盘失败必须回显给用户
            return JSONResponse(
                status_code=500,
                content={"ok": False, "message": f"配置保存失败：{exc}"},
            )

        # ④ 触发隧道平滑重连（内部已兜住所有异常，不会拖垮主进程）
        #    放到线程池执行：reload 会同步等待新连接建立（最长 8s），
        #    直接在事件循环里调用会阻塞本地 HTTP 服务（/health、/v1/chat 等）。
        try:
            result = await asyncio.get_running_loop().run_in_executor(
                None, reload_tunnel_credentials
            )
        except Exception as exc:  # noqa: BLE001 - 热重载异常必须回显给用户
            result = {"ok": False, "connected": False, "reason": str(exc)}
        connected = bool(result.get("connected"))

        return JSONResponse(
            status_code=200,
            content={
                "ok": True,
                "node_id": node_id,
                "connected": connected,
                "message": (
                    "绑定成功，节点已连上云端"
                    if connected
                    else "配置已保存，正在用新凭证重新连接云端…"
                ),
            },
        )

    return app


def _open_ui_browser(host: str, port: int) -> None:
    """在系统默认浏览器打开本地配置面板（失败不影响主进程）。"""
    if os.getenv("APIPAW_NO_UI_BROWSER", "").strip().lower() in ("1", "true", "yes", "on"):
        return
    if host in ("0.0.0.0", "::", ""):
        host = "127.0.0.1"
    url = f"http://{host}:{port}/ui"
    try:
        webbrowser.open(url)
        print(f"[main] 已打开本地配置面板: {url}")
    except Exception as exc:  # noqa: BLE001 - 打开浏览器失败不致命
        print(f"[main] 打开配置面板失败（忽略）: {exc}")


def _simulate_answer(prompt: str, model: str) -> str:
    """
    本地模拟推理结果。

    本仓库分发的是"挂机助手客户端"，不携带模型权重；真实推理由节点侧
    可选的 torch 运行时提供。此处返回结构合法的占位内容，保证 OpenAI 客户端
    链路（SDK / 网关 / 计费）可以完整跑通。
    """
    summary = prompt.strip().replace("\n", " ")[:120]
    return (
        f"[apipaw-agent:{model}] 已收到请求并完成本地调度。\n"
        f"提示词摘要：{summary}\n"
        f"（节点处于挂机状态，随时可接收推理任务。）"
    )


def _chunk_text(text: str, size: int = 24) -> List[str]:
    """把文本切成 SSE 分片。"""
    return [text[i : i + size] for i in range(0, len(text), size)] or [""]


app = create_app()


# ──────────────────────────────────────────────────────────────
# 启动入口
# ──────────────────────────────────────────────────────────────
def _parse_args(argv: Optional[List[str]] = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="apipaw-agent",
        description="APIPaw 挂机节点客户端（本地 OpenAI 规范网关）",
    )
    parser.add_argument("--host", default=None, help="监听地址（默认 127.0.0.1）")
    parser.add_argument("--port", type=int, default=None, help="监听端口（默认 52415）")
    parser.add_argument("--no-tray", action="store_true", help="不启动系统托盘")
    parser.add_argument("--no-telemetry", action="store_true", help="不向云端上报心跳")
    parser.add_argument(
        "--log-level", default=None, choices=["critical", "error", "warning", "info", "debug"],
        help="日志级别",
    )
    return parser.parse_args(argv)


def main(argv: Optional[List[str]] = None) -> int:
    """启动服务。返回进程退出码。"""
    args = _parse_args(argv)
    cfg = get_config()

    host = args.host or cfg.host
    port = args.port or cfg.port
    log_level = (args.log_level or cfg.log_level).lower()

    if args.no_telemetry:
        # 仅本次进程关闭心跳，不写回配置文件
        get_reporter().disable()
    use_tray = (not args.no_tray) and cfg.enable_tray and TRAY_AVAILABLE

    if uvicorn is None:
        print("[main] 缺少 uvicorn，请先执行: pip install -r requirements.txt")
        return 1

    if use_tray:
        # Windows：托盘需要主线程的消息循环，uvicorn 放到后台线程
        server_config = uvicorn.Config(app, host=host, port=port, log_level=log_level)
        server = uvicorn.Server(server_config)
        server_thread = threading.Thread(target=server.run, name="UvicornThread", daemon=True)
        server_thread.start()

        tray = start_tray()
        if tray is not None:
            print("[main] 托盘已启动，退出请右键托盘图标")
            tray.run()
        server.should_exit = True
        server_thread.join(timeout=5.0)
    else:
        uvicorn.run(app, host=host, port=port, log_level=log_level)

    return 0


if __name__ == "__main__":
    sys.exit(main())
