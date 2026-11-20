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
``/api/ollama/tags``                    GET     本机 Ollama 已装模型 + 在线状态
``/api/ollama/recommended``             GET     云端推荐模型列表（免命令行选型）
``/api/ollama/pull``                    POST    免命令行拉取模型（后台线程）
``/api/ollama/bind``                    POST    设为当前接单模型（写盘生效）
======================================  ======  ====================================

让退语义
--------
用户在电脑上操作时节点处于 ``YIELDED``（已让退）状态，此时接口返回 **503**，
让调用方立即把任务转移到其它节点，而不是让本机抢占用户前台。

推理引擎（阶段六 P0 · 模块 A）
------------------------------
配置了推理后端（``inference_backend`` / ``backend_base_url``，默认自动探测
Ollama）时，``/v1/chat/completions`` 走**真实推理**并回传真实 token 用量；
没有可用后端时退回占位实现，并在响应头自报
``X-APIPaw-Inference-Mode: placeholder`` —— 云端据此拒绝把占位文本
当成真答案（阶段六红线 1）。

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
import urllib.parse
import urllib.request
import uuid
import webbrowser
from contextlib import asynccontextmanager
from typing import Any, AsyncIterator, Dict, List, Optional, Tuple

from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, JSONResponse, StreamingResponse
from pydantic import BaseModel, Field
from starlette.middleware.trustedhost import TrustedHostMiddleware

from agent_config import AGENT_VERSION, get_config
from console import enable_utf8_output
import cloud_http
import gateway_failover
import inference_backend
import local_guard
from ollama_local import (
    DEFAULT_OLLAMA_BASE_URL,
    OllamaError,
    PullManager,
    create_model_from_modelfile,
    derive_evolved_model_name,
    list_local_models,
    validate_model_name,
)
from telemetry import collect_gpu_metrics_cached, get_reporter
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
# 阶段二十 · Pipeline Mesh / 多网关容灾的对外快照
# ──────────────────────────────────────────────────────────────
def _mesh_status_payload(cfg: Any, force_refresh: bool = False) -> Dict[str, Any]:
    """Pipeline Mesh 状态快照（任何异常都降级成"没组网"）。"""
    try:
        import mesh_cluster

        return mesh_cluster.mesh_status_from_config(
            cfg, max_age_sec=0.0 if force_refresh else 30.0)
    except Exception as exc:  # noqa: BLE001 - 面板不能因探测失败而报错
        print(f"[agent] Pipeline Mesh 探测失败: {exc}")
        return {
            "mesh_enabled": False,
            "cluster_vram_gb": 0.0,
            "mesh_shards": 0,
            "mesh_rtt_ms": 0.0,
            "mesh_peers_count": 0,
            "layer_splits": [],
            "peers": [],
            "rejected_peers": [],
        }


def _gateway_status_payload() -> Dict[str, Any]:
    """多网关容灾状态快照（WS 隧道为主，心跳为兜底）。"""
    try:
        from ws_tunnel import get_ws_tunnel

        tunnel = get_ws_tunnel()
        if tunnel is not None:
            status = getattr(tunnel, "_failover", None)
            if status is not None:
                return status.status()
    except Exception as exc:  # noqa: BLE001 - 面板不能因隧道未启动而报错
        print(f"[agent] 读取网关容灾状态失败: {exc}")
    try:
        from telemetry import get_reporter

        reporter = get_reporter()
        failover = getattr(reporter, "_failover", None)
        if failover is not None:
            return failover.status()
    except Exception:  # noqa: BLE001
        pass
    return {"gateway_url": "", "gateway_index": 0, "gateway_count": 0,
            "gateway_is_backup": False, "gateways": []}


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


class PrefsRequest(BaseModel):
    """本地配置面板的**运行偏好**（阶段七 P2）。

    - ``backend_model``：本地推理模型（Ollama tag / vLLM 模型名）。
      留空 = 跟随派单载荷。
    - ``force_idle``：强制待命模式（忽略键鼠活动，全天候接单）。
      勾上即**立即生效**（yield_monitor 每次判定都重读配置），无需重启。
    """

    backend_model: Optional[str] = Field(default=None, max_length=256)
    force_idle: Optional[bool] = None


class MeshPrefsRequest(BaseModel):
    """阶段二十 P0/P2 · Pipeline Mesh 与多网关容灾偏好。

    - ``mesh_peers``：局域网协同节点列表（``host:port``）。
    - ``mesh_enabled``：是否启用合体。
    - ``mesh_mode``：分片模式（``Pipeline Shards`` / ``llama.cpp RPC``）。
    - ``mesh_max_rtt_ms``：合体允许的最大 RTT。
    - ``backup_gateway_urls``：备用网关地址列表。
    """

    mesh_peers: Optional[List[str]] = None
    mesh_enabled: Optional[bool] = None
    mesh_mode: Optional[str] = Field(default=None, max_length=64)
    mesh_max_rtt_ms: Optional[float] = Field(default=None, ge=1.0, le=1000.0)
    backup_gateway_urls: Optional[List[str]] = None


class OllamaPullRequest(BaseModel):
    """免命令行拉取本机模型（阶段十三 P2）。"""

    model: str = Field(default="", max_length=256)


class OllamaBindRequest(BaseModel):
    """把本机某个模型设为当前接单模型（阶段十三 P2）。"""

    model: str = Field(default="", max_length=256)


class OllamaEvolveRequest(BaseModel):
    """云端黄金记忆 ⇒ 本机合成进化模型（阶段十五 P2）。

    - ``base_model``：基准模型（留空 = 跟随 ``backend_model``，再兜底
      ``qwen2.5:0.5b``）；
    - ``name``：合成后的模型名（留空 = 由基准模型推导，如
      ``qwen2.5-apipaw-golden:latest``）；
    - ``bind``：合成成功后是否**立刻**设为当前接单模型。
    """

    base_model: str = Field(default="", max_length=256)
    name: str = Field(default="", max_length=256)
    bind: bool = False


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

    # ── 阶段二十二 P1 · Host 闸门（防 DNS Rebinding）────────────────
    # 只监听回环地址**不等于**只有本人能访问：攻击者把 evil.com 的 DNS
    # 指向 127.0.0.1，浏览器就会向本机发请求，而浏览器只按**域名**判同源。
    # ⇒ 只认 127.0.0.1 / localhost 的 Host 头（其余一律 400）。
    _allowed_hosts = local_guard.allowed_hosts(
        getattr(cfg, "host", "") or "", getattr(cfg, "port", 0) or 0)
    app.add_middleware(TrustedHostMiddleware, allowed_hosts=_allowed_hosts)
    _allowed_origins = local_guard.allowed_origins(
        getattr(cfg, "host", "") or "", getattr(cfg, "port", 0) or 0)

    def _guard_local_write(request: Request) -> Optional[JSONResponse]:
        """写端点的 Origin 闸门（返回 None 表示放行）。

        覆盖 ``/api/bind``、``/api/prefs``、``/api/mesh/prefs``、
        ``/api/ollama/pull|bind|evolve`` —— 这些端点能改**节点归属**
        与**备用网关地址**，被 CSRF 拿下等于把节点凭证送人。
        """
        path = str(request.url.path or "")
        if path not in local_guard.LOCAL_WRITE_PATHS:
            return None
        ok, reason = local_guard.check_request_origin(
            dict(request.headers), _allowed_origins)
        if ok:
            return None
        print("[main] 拒绝非本地面板来源的写请求: %s（%s）" % (path, reason))
        return JSONResponse(
            status_code=403,
            content={"ok": False, "code": reason,
                     "message": "请求来源不被信任（本地面板只接受来自本机的请求）"},
        )

    # ── /health ──
    @app.get("/health")
    def health() -> Dict[str, Any]:
        """节点健康检查：返回当前状态、显存、温度与让退指标。

        ⚠️ **刻意是同步端点**：``describe_backend`` 会做**阻塞**的后端探测。
        若写成 ``async def``，这些阻塞调用会跑在事件循环里，把整个节点服务
        拖住（CI 的就绪轮询因此永远超时）。FastAPI 会把同步端点丢进线程池
        执行，事件循环不受影响。
        """
        monitor = get_yield_monitor()
        gpu = collect_gpu_metrics_cached()
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
            # 阶段六 P0 · 模块 A：推理后端状态（有没有真引擎、能跑哪些模型）
            "inference": inference_backend.describe_backend(cfg),
            "gpu": gpu,
            "yield_metrics": monitor.metrics(),
            "telemetry": get_reporter().status(),
            "platform": platform.system(),
            "timestamp": int(time.time()),
        }

    # ── /v1/models ──
    @app.get("/v1/models")
    def list_models() -> Dict[str, Any]:
        """模型列表（OpenAI 规范）。

        阶段六 P0 · 模块 A：有真实推理后端时，**同时**列出后端可用模型
        （``cfg.model_name`` 保留为兜底项，老客户端不受影响）。

        ⚠️ 与 ``/health`` 同理：**同步端点**，避免阻塞探测卡住事件循环。
        """
        data: List[Dict[str, Any]] = [
            {
                "id": cfg.model_name,
                "object": "model",
                "created": int(time.time()),
                "owned_by": "apipaw",
            }
        ]
        backend = inference_backend.get_backend(cfg)
        if backend is not None:
            try:
                for name in backend.list_models(
                        timeout_sec=inference_backend.HEALTH_PROBE_TIMEOUT_SEC)[:64]:
                    if name and name != cfg.model_name:
                        data.append({
                            "id": name,
                            "object": "model",
                            "created": int(time.time()),
                            "owned_by": backend.name,
                        })
            except inference_backend.BackendError as exc:
                print(f"[main] 读取后端模型清单失败: {exc}")
        return {"object": "list", "data": data}

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

        # 阶段六 P0 · 模块 A：有**真实推理后端**时走真推理；
        # 没有 ⇒ 走占位实现（自报 ``X-APIPaw-Inference-Mode: placeholder``，
        # 云端据此拒绝把占位文本当真答案）。
        # ⚠️ 探测是**阻塞**调用 ⇒ 丢到线程池跑，别把事件循环堵住。
        backend = await asyncio.to_thread(inference_backend.get_backend, cfg)
        if backend is not None:
            return await _serve_with_backend(
                backend, request, model, completion_id, created, prompt_tokens)

        if not request.stream:
            content = _simulate_answer(prompt, model)
            return JSONResponse(
                content={
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
                },
                headers={"X-APIPaw-Inference-Mode": "placeholder"},
            )

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
            headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no",
                     "X-APIPaw-Inference-Mode": "placeholder"},
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
        # 阶段十七 P2：GPU 温控读数（走 TTL 缓存，绝不为面板多跑 nvidia-smi）
        thermals: Dict[str, Any] = {"gpu_temp_c": None, "gpu_fan_pct": None}
        try:
            from telemetry import collect_gpu_metrics_cached

            cached_gpu = collect_gpu_metrics_cached()
            thermals["gpu_temp_c"] = cached_gpu.get("gpu_temp_c")
            thermals["gpu_fan_pct"] = cached_gpu.get("gpu_fan_pct")
        except Exception:  # noqa: BLE001 - 采集失败不影响面板
            pass
        return {
            "ok": True,
            "node_id": cfg.node_id,
            "node_name": cfg.node_name,
            "has_secret": bool(cfg.node_secret),
            "ws_connected": is_ws_connected(),
            "state": monitor.state.name,
            "agent_version": AGENT_VERSION,
            # 阶段七 P2：面板要预填这两项（强制待命勾选状态 / 本地推理模型）
            "force_idle": bool(cfg.force_idle),
            "backend_model": str(cfg.backend_model or ""),
            "inference_backend": str(cfg.inference_backend or ""),
            # 阶段十七 P2：GPU 温度 / 风扇转速（None = 本机无独显或不可读）
            "gpu_temp_c": thermals["gpu_temp_c"],
            "gpu_fan_pct": thermals["gpu_fan_pct"],
            # 阶段二十 P0：Pipeline Mesh 状态（合体显存 / 分片 / 层切片表）
            "mesh": _mesh_status_payload(cfg),
            "mesh_peers": list(cfg.mesh_peers),
            "mesh_enabled": bool(cfg.mesh_enabled),
            "mesh_mode": str(cfg.mesh_mode or ""),
            "mesh_max_rtt_ms": float(cfg.mesh_max_rtt_ms or 0.0),
            # 阶段二十 P2：多网关容灾状态
            "gateway": _gateway_status_payload(),
            "backup_gateway_urls": list(cfg.backup_gateway_urls),
            "timestamp": int(time.time()),
        }

    # ── /api/bind ── 绑定凭证：写盘 + 刷新内存 + 隧道平滑重连 ──
    @app.post("/api/bind")
    async def api_bind(req: BindRequest, request: Request) -> JSONResponse:
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

        # ⓪ 来源闸门（阶段二十二 P1）：只接受本地面板发起的绑定
        denied = _guard_local_write(request)
        if denied is not None:
            return denied

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

    # ── 阶段二十 P0/P2 · Pipeline Mesh 与多网关容灾（面板）──
    @app.get("/api/mesh/status")
    async def api_mesh_status() -> Dict[str, Any]:
        """Pipeline Mesh 探测结果（**强制刷新**，不走缓存）。"""
        return {"ok": True, "mesh": _mesh_status_payload(cfg, force_refresh=True)}

    @app.post("/api/mesh/prefs")
    async def api_mesh_prefs(req: MeshPrefsRequest, request: Request) -> JSONResponse:
        """保存 Pipeline Mesh / 多网关容灾配置（写盘即生效，不必重启）。"""
        denied = _guard_local_write(request)
        if denied is not None:
            return denied

        updates: Dict[str, Any] = {}
        if req.mesh_peers is not None:
            peers = [str(p).strip() for p in req.mesh_peers if str(p).strip()]
            updates["mesh_peers"] = peers[:32]
        if req.mesh_enabled is not None:
            updates["mesh_enabled"] = bool(req.mesh_enabled)
        if req.mesh_mode is not None:
            updates["mesh_mode"] = str(req.mesh_mode).strip()[:64]
        if req.mesh_max_rtt_ms is not None:
            updates["mesh_max_rtt_ms"] = float(req.mesh_max_rtt_ms)
        if req.backup_gateway_urls is not None:
            backups = [str(u).strip() for u in req.backup_gateway_urls if str(u).strip()]
            # ⚠️ 阶段二十二 P1：备用网关地址必须过**可信域白名单**
            #    （只允许 wss:// / https:// + *.apipaw.com 等信任域）。
            #    节点会把 Node Secret 发给「网关地址」⇒ 地址可被改 = 凭证外泄。
            rejected = [u for u in backups
                        if not gateway_failover.is_trusted_gateway_url(u)]
            if rejected:
                return JSONResponse(
                    status_code=400,
                    content={
                        "ok": False,
                        "code": "untrusted_gateway_url",
                        "message": ("以下网关地址不被信任（只允许 wss:// / https:// "
                                    "且域名在信任域白名单内）：%s"
                                    % "、".join(rejected[:4])),
                    },
                )
            updates["backup_gateway_urls"] = backups[:8]

        if not updates:
            return JSONResponse(
                status_code=400,
                content={"ok": False, "message": "没有需要保存的配置项"},
            )

        try:
            cfg.update(**updates)
        except Exception as exc:  # noqa: BLE001 - 写盘失败必须回显给用户
            return JSONResponse(
                status_code=500,
                content={"ok": False, "message": f"配置保存失败：{exc}"},
            )

        return JSONResponse(
            status_code=200,
            content={
                "ok": True,
                "mesh": _mesh_status_payload(cfg, force_refresh=True),
                "gateway": _gateway_status_payload(),
                "message": "已保存并生效",
            },
        )

    # ── /api/prefs ── 运行偏好：本地推理模型 + 强制待命（阶段七 P2）──
    @app.post("/api/prefs")
    async def api_prefs(req: PrefsRequest, request: Request) -> JSONResponse:
        """保存运行偏好并**立即生效**（不必重启、不必改 start.bat）。

        - ``backend_model``：本地推理模型 tag；改了之后重置推理后端缓存，
          下一次派单立刻用新模型（否则后端对象还锁在旧模型上）。
        - ``force_idle``：强制待命模式；``yield_monitor`` 每次判定都重读配置，
          因此勾上后**下一次状态轮询**就生效。
        """
        denied = _guard_local_write(request)
        if denied is not None:
            return denied

        updates: Dict[str, Any] = {}
        if req.backend_model is not None:
            updates["backend_model"] = str(req.backend_model).strip()
        if req.force_idle is not None:
            updates["force_idle"] = bool(req.force_idle)

        if not updates:
            return JSONResponse(
                status_code=400,
                content={"ok": False, "message": "没有需要保存的偏好项"},
            )

        try:
            cfg.update(**updates)
        except Exception as exc:  # noqa: BLE001 - 写盘失败必须回显给用户
            return JSONResponse(
                status_code=500,
                content={"ok": False, "message": f"配置保存失败：{exc}"},
            )

        # 换了本地模型 ⇒ 推理后端缓存里的旧对象不再适用，重置它
        if "backend_model" in updates:
            try:
                inference_backend.reset_backend_cache()
            except Exception as exc:  # noqa: BLE001 - 缓存重置失败不致命
                print(f"[agent] 重置推理后端缓存失败: {exc}")

        monitor = get_yield_monitor()
        return JSONResponse(
            status_code=200,
            content={
                "ok": True,
                "backend_model": cfg.backend_model,
                "force_idle": bool(cfg.force_idle),
                "state": monitor.state.name,
                "message": "已保存并生效",
            },
        )

    # ── 阶段十三 P2：免命令行 Ollama 模型管理 ──────────────────────
    # 拉取管理器挂在 app 实例上（每个 app 一个 ⇒ 测试之间互不干扰）
    pull_manager = PullManager()

    def _persist_serving_model(model: str) -> Dict[str, Any]:
        """把模型写成**当前接单模型**（``backend_model`` + ``model_name``）。

        - ``backend_model``：本地推理引擎用的 tag（Ollama/vLLM 都认）；
        - ``model_name``：**对外声明**的服务模型 —— 云端据此把本节点挂到
          该模型名下（阶段七 P1 的自动对齐链路），不改的话派单永远匹配不上。

        ⚠️ 写盘失败必须让调用方看到（回 500），不能静默 —— 否则用户以为
        换好了模型，结果下次派单还是旧模型。
        """
        cfg.update(backend_model=model, model_name=model)
        try:
            inference_backend.reset_backend_cache()
        except Exception as exc:  # noqa: BLE001 - 缓存重置失败不致命
            print(f"[agent] 重置推理后端缓存失败: {exc}")
        return {"backend_model": cfg.backend_model, "model_name": cfg.model_name}

    @app.get("/api/ollama/tags")
    def api_ollama_tags() -> Dict[str, Any]:
        """本机 Ollama 已装模型 + 在线状态 + 当前拉取进度。

        ⚠️ **Ollama 没起来不算错误** —— 返回 ``online=False`` + 原因，
        面板据此显示"本机没装 Ollama"而不是一片红。
        """
        base_url = str(cfg.backend_base_url or DEFAULT_OLLAMA_BASE_URL)
        info = list_local_models(base_url)
        return {
            "ok": True,
            "online": bool(info.get("online")),
            "base_url": base_url,
            "models": info.get("models") or [],
            "error": str(info.get("error") or ""),
            "pull": pull_manager.status(),
            "current_model": str(cfg.backend_model or ""),
            "declared_model": str(cfg.model_name or ""),
            "agent_version": AGENT_VERSION,
        }

    @app.get("/api/ollama/recommended")
    async def api_ollama_recommended() -> JSONResponse:
        """云端推荐模型列表（阶段十三 P2 · ``/api/v1/network/recommended-models``）。

        走 Agent 代理而不是浏览器直连：云端地址在配置里，浏览器直连会踩
        CORS，且面板在无公网的机器上也要能看。
        """
        base = str(cfg.cloud_api_base or "").rstrip("/")
        if not base:
            return JSONResponse(status_code=200, content={
                "ok": False, "models": [], "count": 0,
                "error": "未配置云端地址（cloud_api_base）",
            })
        url = base + "/api/v1/network/recommended-models"

        def _fetch() -> Tuple[Dict[str, Any], bool]:
            # v1.0.19：统一走 cloud_http —— 带合规 UA（否则 Cloudflare 403）、
            # 4s 短超时（不拖住面板）、失败回落内存缓存。
            return cloud_http.fetch_cloud_json_cached(url)

        try:
            data, stale = await asyncio.get_running_loop().run_in_executor(None, _fetch)
        except Exception as exc:  # noqa: BLE001 - 云端不可达不该让面板挂掉
            return JSONResponse(status_code=200, content={
                "ok": False, "models": [], "count": 0,
                "error": "读取云端推荐列表失败：%s" % exc,
            })
        items = data.get("models") if isinstance(data, dict) else None
        return JSONResponse(status_code=200, content={
            "ok": True,
            "models": items or [],
            "count": len(items or []),
            "stale": bool(stale),
            "error": ("云端暂时取不到，下面是上次成功取到的内容" if stale else ""),
        })

    @app.post("/api/ollama/pull")
    async def api_ollama_pull(req: OllamaPullRequest, request: Request) -> JSONResponse:
        """**免命令行**拉取本机模型（阶段十三 P2）。

        流程：校验模型名 → 后台线程调本机 Ollama ``/api/pull`` →
        拉完自动把 ``backend_model`` / ``model_name`` 写进配置文件。

        ⚠️ 拉取是**后台线程**：几百 MB 的下载绝不能占住事件循环，否则
        本机 ``/health`` 会被拖死、面板会超时。
        """
        denied = _guard_local_write(request)
        if denied is not None:
            return denied
        try:
            model = validate_model_name(req.model)
        except ValueError as exc:
            return JSONResponse(status_code=400, content={
                "ok": False, "started": False, "message": str(exc)})
        base_url = str(cfg.backend_base_url or DEFAULT_OLLAMA_BASE_URL)

        def _on_success(pulled: str) -> None:
            # 落盘由**端点层**做（不是 ollama_local 模块）——
            # 这样单测可以注入临时配置，绝不污染真机 agent.config.json。
            try:
                _persist_serving_model(pulled)
                print("[ollama] 已拉取 %s 并设为当前接单模型" % pulled)
            except Exception as exc:  # noqa: BLE001 - 落盘失败不影响拉取结论
                print("[ollama] 拉取成功但写入配置失败: %s" % exc)

        try:
            result = pull_manager.start(model, base_url=base_url,
                                        on_success=_on_success)
        except ValueError as exc:
            return JSONResponse(status_code=400, content={
                "ok": False, "started": False, "message": str(exc)})
        except OllamaError as exc:
            return JSONResponse(status_code=502, content={
                "ok": False, "started": False, "message": str(exc)})
        if not result.get("started"):
            reason = str(result.get("reason") or "")
            return JSONResponse(status_code=409, content={
                "ok": False, "started": False, "model": model,
                "message": ("已有拉取任务在进行中，请稍候"
                            if reason == "busy" else "拉取未启动"),
                "pull": pull_manager.status()})
        return JSONResponse(status_code=200, content={
            "ok": True, "started": True, "model": model,
            "message": "已开始拉取 %s，可在本页查看进度" % model,
            "pull": pull_manager.status()})

    @app.post("/api/ollama/bind")
    async def api_ollama_bind(req: OllamaBindRequest, request: Request) -> JSONResponse:
        """把**本机已装**的模型设为当前接单模型（不下载，立刻生效）。"""
        denied = _guard_local_write(request)
        if denied is not None:
            return denied
        try:
            model = validate_model_name(req.model)
        except ValueError as exc:
            return JSONResponse(status_code=400, content={
                "ok": False, "message": str(exc)})
        base_url = str(cfg.backend_base_url or DEFAULT_OLLAMA_BASE_URL)
        info = list_local_models(base_url)
        installed = {str(m.get("name") or "") for m in (info.get("models") or [])}
        # Ollama 会在 tags 里返回 `name:tag`，也可能是裸 `name` —— 两种都认
        if model not in installed and model.split(":")[0] not in installed:
            if not info.get("online"):
                return JSONResponse(status_code=503, content={
                    "ok": False,
                    "message": "本机 Ollama 未运行（%s），无法确认模型是否已装"
                               % (info.get("error") or "未连接")})
            return JSONResponse(status_code=400, content={
                "ok": False,
                "message": "本机未安装该模型，请先「一键拉取并绑定」"})
        try:
            saved = _persist_serving_model(model)
        except Exception as exc:  # noqa: BLE001 - 写盘失败必须回显
            return JSONResponse(status_code=500, content={
                "ok": False, "message": "配置保存失败：%s" % exc})
        return JSONResponse(status_code=200, content={
            "ok": True, "model": model,
            "backend_model": saved.get("backend_model"),
            "model_name": saved.get("model_name"),
            "message": "已设为当前接单模型，下一次派单立即生效"})

    # ── 阶段十五 P2：云端黄金记忆 ⇒ 本机一键合成进化模型 ──────────────
    @app.post("/api/ollama/evolve")
    async def api_ollama_evolve(req: OllamaEvolveRequest, request: Request) -> JSONResponse:
        """**一键合成进化模型**（阶段十五 P2 · 终极闭环的本地半边）。

        流程：

        1. 从云端公开端点拉 **标准 Ollama Modelfile**
           （``/api/v1/network/golden-modelfile``，只含脱敏后的公开黄金语料）；
        2. 在**本机** Ollama 上 ``POST /api/create`` 合成派生模型
           （零训练成本：把黄金 Few-Shot 直接写进 system / messages）；
        3. 可选：把新模型写成当前接单模型（``backend_model`` + ``model_name``）。

        ⚠️ 本机 Ollama 没起来 ⇒ 回 **200 + ``ok:false`` + 人话提示**
        （不是 500）—— 面板要能直接把这句话显示给用户。
        """
        denied = _guard_local_write(request)
        if denied is not None:
            return denied
        base = str(cfg.cloud_api_base or "").rstrip("/")
        if not base:
            return JSONResponse(status_code=200, content={
                "ok": False, "evolved": False,
                "message": "未配置云端地址（cloud_api_base），无法拉取云端黄金记忆"})
        base_model = str(req.base_model or "").strip() \
            or str(cfg.backend_model or "").strip() \
            or "qwen2.5:0.5b"
        try:
            validate_model_name(base_model)
        except ValueError as exc:
            return JSONResponse(status_code=400, content={
                "ok": False, "evolved": False, "message": str(exc)})
        name = str(req.name or "").strip() or derive_evolved_model_name(base_model)
        try:
            validate_model_name(name)
        except ValueError as exc:
            return JSONResponse(status_code=400, content={
                "ok": False, "evolved": False, "message": str(exc)})

        url = "%s/api/v1/network/golden-modelfile?base_model=%s" % (
            base, urllib.parse.quote(base_model, safe=""))

        def _fetch() -> str:
            # v1.0.19：走 cloud_http（合规 UA + 退避重试），避免 Cloudflare 403
            return cloud_http.fetch_cloud_text(
                url, accept="text/plain", timeout=15.0, retries=2)

        def _fetch_summary() -> Dict[str, Any]:
            data, _stale = cloud_http.fetch_cloud_json_cached(
                base + "/api/v1/network/golden-summary")
            return data

        try:
            modelfile_text = await asyncio.get_running_loop().run_in_executor(None, _fetch)
        except Exception as exc:  # noqa: BLE001 - 云端不可达要给用户一句人话
            return JSONResponse(status_code=200, content={
                "ok": False, "evolved": False,
                "message": "读取云端黄金记忆失败：%s" % exc})
        try:
            summary = await asyncio.get_running_loop().run_in_executor(
                None, _fetch_summary)
        except Exception:  # noqa: BLE001 - 摘要只是锦上添花
            summary = {}

        ollama_base = str(cfg.backend_base_url or DEFAULT_OLLAMA_BASE_URL)
        result = await asyncio.get_running_loop().run_in_executor(
            None, create_model_from_modelfile,
            ollama_base, name, modelfile_text)
        if not result.get("ok"):
            message = str(result.get("error") or "合成失败")
            if "无法连接本机 Ollama" in message:
                message = ("本机 Ollama 未运行（%s），请先启动 Ollama 再试"
                           % ollama_base)
            return JSONResponse(status_code=200, content={
                "ok": False, "evolved": False, "name": name,
                "base_model": base_model, "message": message,
                "golden_count": int(summary.get("golden_count") or 0)})

        bound = False
        saved: Dict[str, Any] = {}
        if req.bind:
            try:
                saved = _persist_serving_model(name)
                bound = True
            except Exception as exc:  # noqa: BLE001 - 写盘失败必须回显
                return JSONResponse(status_code=500, content={
                    "ok": False, "evolved": True, "name": name,
                    "message": "模型已合成，但写入配置失败：%s" % exc})
        print("[ollama] 已合成进化模型 %s（黄金范例 %s 条）"
              % (name, result.get("messages")))
        return JSONResponse(status_code=200, content={
            "ok": True, "evolved": True, "name": name,
            "base_model": base_model, "from": result.get("from"),
            "few_shot_messages": int(result.get("messages") or 0),
            "system_chars": int(result.get("system") or 0),
            "golden_count": int(summary.get("golden_count") or 0),
            "bound": bound,
            "backend_model": saved.get("backend_model", ""),
            "model_name": saved.get("model_name", ""),
            "message": ("已合成 %s%s" % (
                name, "，并设为当前接单模型" if bound else "")),
        })

    @app.get("/api/ollama/golden-summary")
    async def api_ollama_golden_summary() -> JSONResponse:
        """云端黄金记忆摘要（公开端点代理）—— 面板在合成前先给用户看一眼。"""
        base = str(cfg.cloud_api_base or "").rstrip("/")
        if not base:
            return JSONResponse(status_code=200, content={
                "ok": False, "golden_count": 0, "error": "未配置云端地址"})

        def _fetch() -> Tuple[Dict[str, Any], bool]:
            # v1.0.19：统一走 cloud_http（合规 UA + 4s 短超时 + 缓存兜底）
            return cloud_http.fetch_cloud_json_cached(
                base + "/api/v1/network/golden-summary")

        try:
            data, stale = await asyncio.get_running_loop().run_in_executor(None, _fetch)
        except Exception as exc:  # noqa: BLE001
            return JSONResponse(status_code=200, content={
                "ok": False, "golden_count": 0, "error": str(exc)})
        return JSONResponse(status_code=200, content={
            "ok": True,
            "golden_count": int(data.get("golden_count") or 0),
            "pool_total": int(data.get("pool_total") or 0),
            "latest": data.get("latest") or [],
            "stale": bool(stale),
            "error": ("云端暂时取不到，下面是上次成功取到的内容" if stale else "")})

    return app


async def _serve_with_backend(backend: Any, request: ChatCompletionRequest, model: str,
                              completion_id: str, created: int,
                              prompt_tokens: int) -> Any:
    """用**真实推理后端**回答 ``/v1/chat/completions``（阶段六 P0 · 模块 A）。

    - 非流式：收齐全文后一次性返回，usage 优先用后端回报的**真实** token；
    - 流式：后端分片实时转成 SSE（真流式，不是"先算完再切片"）；
    - 后端中途不可用：流式已开只能断流（结束帧不带 usage ⇒ 云端按估算口径），
      非流式则明确回 **503 / backend_unavailable**（绝不回落占位文本）。
    """
    messages = [{"role": m.role, "content": m.content} for m in request.messages]
    monitor = get_yield_monitor()

    async def _iter_backend():
        async for chunk in backend.generate(
            model,
            messages,
            temperature=request.temperature,
            top_p=request.top_p,
            max_tokens=request.max_tokens,
        ):
            yield chunk

    if not request.stream:
        parts: List[str] = []
        usage_payload: Optional[Dict[str, int]] = None
        try:
            async for chunk in _iter_backend():
                if chunk.text:
                    parts.append(chunk.text)
                if chunk.usage is not None:
                    usage_payload = chunk.usage.to_dict()
        except inference_backend.BackendUnavailable as exc:
            return JSONResponse(
                status_code=503,
                content={"error": {"message": str(exc), "type": "apipaw_backend_error",
                                   "code": "backend_unavailable", "param": None}},
                headers={"X-APIPaw-Inference-Mode": "backend"},
            )
        except Exception as exc:  # noqa: BLE001
            return JSONResponse(
                status_code=502,
                content={"error": {"message": str(exc), "type": "apipaw_backend_error",
                                   "code": "backend_error", "param": None}},
                headers={"X-APIPaw-Inference-Mode": "backend"},
            )
        content = "".join(parts)
        if usage_payload is None:
            usage_payload = build_usage(prompt_tokens, count_tokens(content))
        return JSONResponse(
            content={
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
                "usage": usage_payload,
            },
            headers={"X-APIPaw-Inference-Mode": "backend"},
        )

    async def event_stream() -> AsyncIterator[str]:
        emitted = ""
        usage_payload = None
        try:
            async for chunk in _iter_backend():
                if not monitor.is_idle:
                    break
                if chunk.text:
                    emitted += chunk.text
                    payload = {
                        "id": completion_id,
                        "object": "chat.completion.chunk",
                        "created": created,
                        "model": model,
                        "choices": [
                            {"index": 0, "delta": {"content": chunk.text},
                             "finish_reason": None}
                        ],
                    }
                    yield f"data: {json.dumps(payload, ensure_ascii=False)}\n\n"
                if chunk.usage is not None:
                    usage_payload = chunk.usage.to_dict()
        except Exception as exc:  # noqa: BLE001 - 流已开始，只能断流
            print(f"[main] 推理后端异常（流式中断）: {exc}")
        if usage_payload is None:
            usage_payload = build_usage(prompt_tokens, count_tokens(emitted))
        final = {
            "id": completion_id,
            "object": "chat.completion.chunk",
            "created": created,
            "model": model,
            "choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}],
            "usage": usage_payload,
        }
        yield f"data: {json.dumps(final, ensure_ascii=False)}\n\n"
        yield "data: [DONE]\n\n"

    return StreamingResponse(
        event_stream(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no",
                 "X-APIPaw-Inference-Mode": "backend"},
    )


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
