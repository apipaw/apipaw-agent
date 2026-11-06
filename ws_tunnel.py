"""
ws_tunnel.py — WebSocket 云端隧道（APIPaw 节点的主通信通道）

职责：
1. 与云端 ``wss://cloud.apipaw.com/api/v1/node/stream`` 建立 WSS 长连接
2. 连接后先发送一帧 ``INIT`` 完成节点注册，随后周期上报 ``HEARTBEAT`` 帧
3. 云端关闭连接时**静默退避重连**，直到再次出现 ``✅ WebSocket 已连接``
4. 接收云端下发的 ``INFERENCE_REQUEST``：节点空闲时转发到本地 FastAPI，
   非空闲时回 ``RESPONSE_ERROR``（503 / ``node_yielded``）
5. 与 HTTP 心跳互为备份：WS 在线时由 WS 承担心跳上报，HTTP 仅作兜底

设计要点：
- ``websockets`` / ``requests`` 都是**可选依赖**：缺失时隧道降级为空闲，
  节点依旧可以启动、跑本地推理接口（CI 的 minimal 矩阵必须能通过）。
- 隧道跑在独立线程的 asyncio 事件循环里，不阻塞 uvicorn / 托盘主线程。
"""

from __future__ import annotations

import asyncio
import inspect
import json
import threading
import time
from typing import Any, Dict, Optional, Tuple

from agent_config import AGENT_VERSION, get_config
from console import enable_utf8_output
from yield_monitor import get_yield_monitor

enable_utf8_output()

try:  # WebSocket 主通道依赖（缺失时降级，不影响节点其它能力）
    import websockets
except ImportError as exc:  # pragma: no cover - 依赖缺失时降级
    websockets = None  # type: ignore[assignment]
    _WEBSOCKETS_IMPORT_ERROR = str(exc)
else:
    _WEBSOCKETS_IMPORT_ERROR = None

try:  # 仅在转发云端推理请求时使用
    import requests
except ImportError as exc:  # pragma: no cover
    requests = None  # type: ignore[assignment]
    _REQUESTS_IMPORT_ERROR = str(exc)
else:
    _REQUESTS_IMPORT_ERROR = None


try:  # websockets >= 14 把 ConnectionClosed 放在 exceptions 子模块
    from websockets.exceptions import ConnectionClosed
except ImportError:  # pragma: no cover - 旧版 websockets
    try:
        from websockets import ConnectionClosed  # type: ignore[attr-defined]
    except ImportError:
        class ConnectionClosed(Exception):  # type: ignore[no-redef]
            """占位异常：websockets 缺失时不会被触发。"""


# ──────────────────────────────────────────────────────────────
# 消息类型常量（与云端网关 /api/v1/node/stream 约定一致）
# ──────────────────────────────────────────────────────────────
TYPE_INIT = "INIT"
TYPE_HEARTBEAT = "HEARTBEAT"
TYPE_NODE_OFFLINE = "NODE_OFFLINE"
TYPE_INFERENCE_REQUEST = "INFERENCE_REQUEST"
TYPE_RESPONSE_CHUNK = "RESPONSE_CHUNK"
TYPE_RESPONSE_DONE = "RESPONSE_DONE"
TYPE_RESPONSE_ERROR = "RESPONSE_ERROR"
# 应用层保活（DAY14，与云端网关 server.py 配套）：
#   云端每 20s 发一帧 {"type":"PING"}，节点回 {"type":"PONG"}。
#   用**数据帧**而不是只依赖 ASGI 控制帧：控制帧不携带应用数据，
#   Cloudflare / 中间设备不据此判定链路活跃，容易把长连接当空闲复位（1006）。
TYPE_PING = "PING"
TYPE_PONG = "PONG"

# 链路看门狗（DAY14）：侦测"静默死亡"的半开连接并主动重连。
# 半开（TCP 半开，没有 RST，两端都收不到 FIN）在跨公网链路上非常常见：
# 边缘层只掐掉"源站侧"那条腿，客户端这条腿看起来一切正常，
# 于是节点"活着"却既收不到派单、也收不到任何下行帧。
# 两道探针互补：
#   1) 控制层：每 10s 主动发一个 WS PING，30s 内收不到 PONG → 判定链路已死。
#      （与"云端是否支持应用层保活"无关，老网关同样受保护）
#   2) 应用层：确认云端支持应用层保活后，若 120s 内没有任何**下行数据帧** → 判死。
#      云端每 20s 一帧应用层 PING，120s = 容忍连续 6 帧丢失，跨公网抖动不会误杀。
# 阈值都比操作系统的 TCP 超时（Windows 可达数分钟~数小时）快一个数量级。
WS_WATCHDOG_TIMEOUT_SEC = 120.0
WS_WATCHDOG_CHECK_INTERVAL_SEC = 10.0
WS_PONG_WATCHDOG_TIMEOUT_SEC = 30.0


def _is_closed(conn: Any) -> bool:
    """安全检测连接是否已关闭（兼容新旧版 websockets）。"""
    if conn is None:
        return True
    if getattr(conn, "closed", False):
        return True
    return getattr(conn, "close_code", None) is not None


def _connect_kwargs(headers: Dict[str, str]) -> Dict[str, Any]:
    """按 websockets 版本选择请求头参数名（extra_headers / additional_headers）。"""
    kwargs: Dict[str, Any] = {
        # 每 10s 发一个 WebSocket Ping 控制帧，作用是**保持链路活跃**，
        # 防止中间层（Cloudflare / nginx）把连接判定为空闲而掐断。
        "ping_interval": 10.0,
        # ping_timeout=None：客户端**不因等不到 pong 而主动断链**。
        # 跨公网 + Cloudflare 抖动时 pong 迟到是常态，一旦客户端拥有"自杀权"，
        # 连接寿命会被打散成大量 20s 级短连接（2026-11-05 实测：真实客户端
        # 20~190s 必断，而与服务端同版库的对照组 4.5 分钟零断连）。
        # 死亡探测交给服务端 keepalive 与节点注册表的存活窗口。
        "ping_timeout": None,
        "max_size": 10 * 1024 * 1024,
        # 关闭握手最多等 2s：退出时不能被慢速网络拖住
        "close_timeout": 2.0,
    }
    try:
        params = inspect.signature(websockets.connect).parameters
    except (TypeError, ValueError):  # pragma: no cover - 签名不可读时用新式参数
        params = {}
    if "additional_headers" in params:
        kwargs["additional_headers"] = headers
    else:  # websockets < 14
        kwargs["extra_headers"] = headers
    if "open_timeout" in params:
        # 握手超时显式收紧：Cloudflare 链路抖动时不让 connect() 长时间挂起
        kwargs["open_timeout"] = 10.0
    return kwargs


class WebSocketTunnel:
    """
    WebSocket 隧道客户端。

    运行在独立线程的 asyncio 事件循环中，负责：
    - 长连接管理与静默退避重连
    - 周期心跳上报
    - 接收云端推理请求并转发到本地 FastAPI
    """

    def __init__(self) -> None:
        self._cfg = get_config()
        self._ws_url = self._cfg.cloud_ws_url
        self._node_id = self._cfg.node_id
        self._node_secret = self._cfg.node_secret

        self._ws: Optional[Any] = None
        self._running = False
        self._connected = False
        self._task: Optional[asyncio.Task] = None
        self._hb_task: Optional[asyncio.Task] = None
        self._loop: Optional[asyncio.AbstractEventLoop] = None
        self._thread: Optional[threading.Thread] = None

        # 重连参数（指数退避 1s → 2s → 4s … 上限 8s）
        # 上限从 60s 降到 8s：节点离线 40s 以上对派单是致命的
        #（2026-11-05 实测重连间隔曾达 43s，期间所有派单都只能回退模拟回答）
        self._base_delay = 1.0
        self._max_delay = 8.0
        self._current_delay = 1.0

        # 心跳参数
        self._heartbeat_interval = float(self._cfg.heartbeat_interval_seconds or 10.0)

        # 统计
        self._total_completed_tokens = 0

        # 最近一次收到云端**任何**帧的时刻（DAY14 链路看门狗基准）
        self._last_server_activity = 0.0
        # 链路看门狗任务
        self._watchdog_task: Optional[asyncio.Task] = None
        # 是否已收到过云端的应用层 PING（= 云端确认支持应用层保活）
        # 只有确认支持后看门狗才敢武装，避免与旧版网关搭配时误杀健康连接
        self._server_app_ping_seen = False

        print(f"[ws_tunnel] 初始化完成，云端地址: {self._ws_url}")

    # ───────────────────────── 公开 API ─────────────────────────

    def start(self) -> None:
        """在独立线程中启动 asyncio 主循环。"""
        if websockets is None:
            print(f"[ws_tunnel] 缺少 websockets 依赖，WebSocket 主通道不可用: {_WEBSOCKETS_IMPORT_ERROR}")
            return
        if self._running:
            print("[ws_tunnel] 已在运行，忽略重复启动")
            return
        self._running = True
        self._thread = threading.Thread(
            target=self._run_asyncio_loop, name="WsTunnelThread", daemon=True
        )
        self._thread.start()
        print("[ws_tunnel] 已启动（独立线程）")

    def stop(self) -> None:
        """停止隧道：先通知云端节点下线，再关闭 WebSocket 并回收线程。"""
        self._running = False

        # Step 1: 主动断连通知（NODE_OFFLINE），在事件循环线程内发送
        self._notify_offline("shutdown")

        # Step 2: 取消心跳与连接任务
        if self._hb_task:
            self._hb_task.cancel()
            self._hb_task = None
        if self._task:
            self._task.cancel()

        # Step 3: 关闭底层连接（等待关闭握手完成，避免残留 keepalive 任务）
        loop = self._loop
        if loop is not None and loop.is_running() and self._ws is not None:
            try:
                asyncio.run_coroutine_threadsafe(self._ws.close(), loop).result(timeout=3.0)
            except Exception:  # noqa: BLE001 - 关闭失败不阻塞退出
                pass

        # Step 4: 等待事件循环处理完取消（避免 "Task was destroyed"）
        deadline = time.time() + 6.0
        while time.time() < deadline:
            if self._task is None or self._task.done():
                break
            time.sleep(0.05)

        # Step 5: 停止事件循环并回收线程
        if self._loop and self._loop.is_running():
            self._loop.call_soon_threadsafe(self._loop.stop)
        if self._thread:
            self._thread.join(timeout=5.0)
            self._thread = None
        self._task = None
        self._ws = None
        self._connected = False
        print("[ws_tunnel] 已停止")

    def is_connected(self) -> bool:
        """WebSocket 主通道当前是否在线（供 HTTP 心跳让位判断）。"""
        return bool(self._connected and not _is_closed(self._ws))

    # ───────────────────────── 内部 asyncio 任务 ─────────────────────────

    def _run_asyncio_loop(self) -> None:
        """在独立线程中运行 asyncio 事件循环。"""
        self._loop = asyncio.new_event_loop()
        asyncio.set_event_loop(self._loop)
        self._task = self._loop.create_task(self._connect_loop())
        try:
            self._loop.run_forever()
        finally:
            try:
                self._loop.close()
            except Exception:  # pragma: no cover - 关闭失败不影响退出
                pass
            self._loop = None

    async def _connect_loop(self) -> None:
        """
        主连接循环：
        - 云端主动关闭 → 打印 "云端关闭连接" 后**静默退避重连**
        - 其它异常 → 打印原因后退避重连
        - 连接成功后重置退避延迟，并启动心跳协程

        DAY14：区分两类失败，决定退避策略
        ------------------------------------------------------
        1) **握手前失败**（TCP 连不上 / 握手超时 / 被边缘层拒绝）：
           这是 Cloudflare 边缘的瞬时拒绝，**与链路拥塞无关**。
           2026-11-06 实测：一次节点离线空窗里 CF 连续拒了 3~4 次握手，
           而旧策略把退避 1→2→4→8 翻倍，等于"越失败越等越久"，
           50s 的空窗里只挤进 4 次尝试。现在握手失败**不翻倍**，
           保持 1s 起步连续重试，同等时间可尝试 10 次以上。
        2) **连上之后掉线**（1006 / 看门狗判死）：
           这才是链路真的有问题，继续指数退避（上限 8s），
           避免在链路拥塞时把源站打爆。
        """
        while self._running:
            established = False
            try:
                print(f"[ws_tunnel] 正在连接云端: {self._ws_url}")
                kwargs = _connect_kwargs(self._build_auth_headers())
                async with websockets.connect(self._ws_url, **kwargs) as ws:
                    established = True
                    self._ws = ws
                    self._connected = True
                    self._current_delay = self._base_delay  # 重置退避延迟
                    self._last_server_activity = time.time()
                    self._server_app_ping_seen = False
                    print("[ws_tunnel] ✅ WebSocket 已连接")
                    await self._send_init()
                    self._hb_task = self._loop.create_task(self._heartbeat_loop())
                    self._watchdog_task = self._loop.create_task(self._watchdog_loop())
                    try:
                        await self._handle_messages()
                    finally:
                        if self._hb_task:
                            self._hb_task.cancel()
                            self._hb_task = None
                        if self._watchdog_task:
                            self._watchdog_task.cancel()
                            self._watchdog_task = None
                self._connected = False
                if not self._running:
                    return
                # 连接体正常结束（未抛异常）：链路是通的 → 立即重连，不翻倍退避
                print(f"[ws_tunnel] 云端关闭连接 {self._describe_close(None)}（立即重连）")
                await self._silent_backoff(keep_delay=True)
            except asyncio.CancelledError:
                print("[ws_tunnel] 任务取消，退出连接循环")
                return
            except KeyboardInterrupt:  # pragma: no cover
                print("[ws_tunnel] 收到 KeyboardInterrupt，退出连接循环")
                self._running = False
                return
            except ConnectionClosed as exc:
                # 云端关闭连接：静默退避重连，直到再次显示 "✅ WebSocket 已连接"
                self._connected = False
                if not self._running:
                    return
                clean = self._is_clean_close(exc)
                print("[ws_tunnel] 云端关闭连接 %s%s"
                      % (self._describe_close(exc),
                         "（正常关闭，立即重连）" if clean else ""))
                # 正常关闭（1000/1001）说明链路本身是通的 → 不翻倍退避；
                # 异常关闭（如 1006 对端无感知地断开）才继续指数退避。
                await self._silent_backoff(keep_delay=clean)
            except Exception as exc:  # noqa: BLE001 - 任何异常都必须能自愈
                self._connected = False
                if not self._running:
                    print("[ws_tunnel] 已停止，不再重连")
                    return
                # 握手前失败（边缘层拒绝/握手超时）**不翻倍退避**：
                # 连续快速重试能在同样的空窗里挤出更多次尝试（DAY14 实测 CF 会连续拒 3~4 次）。
                phase = "握手阶段" if not established else "连接阶段"
                print(f"[ws_tunnel] 连接异常（{phase}）: {exc}，"
                      f"{self._current_delay:.1f}s 后重连...")
                await asyncio.sleep(self._current_delay)
                if established:
                    # 连上之后掉线 = 链路真有问题 → 继续指数退避
                    self._current_delay = min(self._current_delay * 2, self._max_delay)
                else:
                    # 握手阶段失败 = 边缘层瞬时拒绝 → 退避保持在起步值
                    self._current_delay = self._base_delay

    async def _silent_backoff(self, keep_delay: bool = False) -> None:
        """静默退避：只等待，不打印任何错误噪音。

        ``keep_delay=True`` 时保持当前退避时长不翻倍 —— 用于"连接正常关闭"场景：
        链路本身是通的（对端或中间层主动断开），此时翻倍退避只会让节点白白离线更久，
        直接影响云端派单命中率。
        """
        await asyncio.sleep(self._current_delay)
        if not keep_delay:
            self._current_delay = min(self._current_delay * 2, self._max_delay)

    def _describe_close(self, exc: Any) -> str:
        """把断连渲染成可观测的 close code / reason 描述。

        没有异常对象时（例如连接体自然结束），从连接对象上读 ``close_code``。
        断连码是排查"谁在掐连接"的唯一直接证据：
        1000/1001 = 正常关闭，1006 = 对端无感知断开（网络/边缘层重置），
        1011 = 协议内部错误（如 keepalive pong 超时）。
        """
        code: Any = getattr(exc, "code", None) if exc is not None else None
        reason: Any = getattr(exc, "reason", None) if exc is not None else None
        if code is None and self._ws is not None:
            code = getattr(self._ws, "close_code", None)
            reason = getattr(self._ws, "close_reason", None) or reason
        if code is None:
            return "code=<未知>"
        return "code=%s reason=%r" % (code, reason or "")

    @staticmethod
    def _is_clean_close(exc: Any) -> bool:
        """1000（正常关闭）/ 1001（离开）视为"链路正常终止"。"""
        return getattr(exc, "code", None) in (1000, 1001)

    async def _send_init(self) -> None:
        """连接建立后发送注册帧（云端据此把节点写入注册表）。"""
        monitor = get_yield_monitor()
        await self._send_json(self._build_frame(TYPE_INIT, monitor))

    async def _heartbeat_loop(self) -> None:
        """周期心跳：连接建立后按 ``heartbeat_interval_seconds`` 上报节点状态。"""
        while self._running and self._ws is not None:
            try:
                await asyncio.sleep(self._heartbeat_interval)
            except asyncio.CancelledError:
                break

            if self._ws is None or _is_closed(self._ws):
                break

            try:
                monitor = get_yield_monitor()
                await self._send_json(self._build_frame(TYPE_HEARTBEAT, monitor))
                print(
                    f"[ws_tunnel]  心跳已发送 | 状态: {monitor.state.name} "
                    f"| Tokens: {self._total_completed_tokens}"
                )
            except Exception as exc:  # noqa: BLE001 - 心跳失败交给连接循环重连
                print(f"[ws_tunnel] 心跳发送失败: {exc}")
                break

    async def _watchdog_loop(self) -> None:
        """链路看门狗：侦测"静默死亡"的半开连接并主动重连（DAY14）。

        为什么需要它：ASGI 的 ``ping_timeout=None`` 让客户端**永远不会**因为
        收不到 pong 而断链（这是 L3 修复刻意为之，避免跨公网抖动时自杀）。
        但若链路在中间层被静默掐断（TCP 半开：没有 RST，两端都收不到 FIN），
        客户端将永远以为连接还在 —— 节点看起来"在线"，实际既收不到派单也发不出心跳。
        2026-11-06 线上实测过这种形态：服务端在连接建立 10s 后记录到 1006，
        而客户端那一侧完全没收到任何 FIN/RST，此后 8 分钟都没有重连。

        两道互补的探针：
          1) **控制层**：每 10s 主动发一个 WS PING，30s 内收不到 PONG → 判死。
             与云端版本无关（老网关也会回 PONG），因此是最可靠的一道。
          2) **应用层**：确认云端支持应用层保活后，若 120s 内没有收到任何
             **下行数据帧** → 判死（云端每 20s 一帧应用层 PING）。
        """
        try:
            while self._running and self._ws is not None:
                await asyncio.sleep(WS_WATCHDOG_CHECK_INTERVAL_SEC)
                ws = self._ws
                if ws is None or _is_closed(ws):
                    break

                # ---- 探针 1：控制层 PING/PONG ----
                # websockets 17.x 的语义：``await ws.ping()`` 返回的是一个
                # **等待 PONG 的 future**（而不是延迟值），必须再 await 一次。
                # 这里用 asyncio.wait_for 给它加上自己的截止时间
                # （Agent 自身的 ping_timeout=None 永远不会超时）。
                dead_reason = ""
                try:
                    pong_waiter = await ws.ping()
                    await asyncio.wait_for(pong_waiter,
                                           timeout=WS_PONG_WATCHDOG_TIMEOUT_SEC)
                except asyncio.TimeoutError:
                    dead_reason = (f"{WS_PONG_WATCHDOG_TIMEOUT_SEC:.0f}s 未收到服务端 PONG"
                                   "（判定为半开死链）")
                except asyncio.CancelledError:
                    raise
                except Exception:  # noqa: BLE001 - 连接本身已死 → 交给主循环重连
                    break

                # ---- 探针 2：应用层下行数据帧静默 ----
                if not dead_reason and self._server_app_ping_seen:
                    idle = time.time() - self._last_server_activity
                    if idle >= WS_WATCHDOG_TIMEOUT_SEC:
                        dead_reason = (f"{idle:.0f}s 未收到云端任何数据帧"
                                       "（判定为半开死链）")

                if not dead_reason:
                    continue

                print(f"[ws_tunnel] ⚠️ 链路看门狗触发：{dead_reason}，主动断开重连")
                try:
                    await ws.close()
                except Exception:  # noqa: BLE001 - 关闭失败也要继续走重连流程
                    pass
                return
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001 - 看门狗自身异常不能拖垮主循环
            print(f"[ws_tunnel] 链路看门狗异常: {exc}")

    def _build_frame(self, frame_type: str, monitor: Any) -> Dict[str, Any]:
        """组装心跳 / 注册帧。"""
        cfg = self._cfg
        return {
            "type": frame_type,
            "node_id": cfg.node_id,
            "node_name": cfg.node_name,
            "status": monitor.state.name,
            "status_label": monitor.state_label,
            "hardware_type": cfg.hardware_type,
            "device": cfg.device,
            "max_vram": cfg.max_vram_gb,
            "completed_tokens": self._total_completed_tokens,
            "agent_version": AGENT_VERSION,
            "timestamp": time.time(),
            # DAY14：声明支持**应用层** PING/PONG 保活。
            # 云端收到该标记后才启用"60s 收不到 PONG 即判死"的回收逻辑；
            # 老版本客户端不带此字段 → 云端保持原有行为，完全向后兼容。
            "app_ping": 1,
        }

    async def _handle_messages(self) -> None:
        """消息接收循环：转发云端下发的推理请求。"""
        while self._running and self._ws is not None:
            try:
                raw_msg = await self._ws.recv()
            except ConnectionClosed:
                # 交给 _connect_loop 统一处理"云端关闭连接"与静默重连
                raise
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001
                print(f"[ws_tunnel] 消息接收异常: {exc}")
                return
            # DAY14：任何下行**数据帧**都视为链路存活证据（看门狗基准）
            self._last_server_activity = time.time()
            await self._handle_message(raw_msg)

    async def _handle_message(self, raw_msg: Any) -> None:
        """处理云端下发消息。"""
        try:
            if isinstance(raw_msg, (bytes, bytearray)):
                raw_msg = raw_msg.decode("utf-8")
            msg = json.loads(raw_msg)
        except Exception as exc:  # noqa: BLE001
            print(f"[ws_tunnel] 消息解析失败: {exc}，原始: {raw_msg!r}")
            return

        msg_type = msg.get("type", "")
        if msg_type == TYPE_PING:
            # DAY14 应用层保活：立即回 PONG，让云端确认这条链路还活着
            self._server_app_ping_seen = True
            await self._send_json({"type": TYPE_PONG, "ts": time.time()})
            return
        if msg_type == TYPE_PONG:
            return
        if msg_type == TYPE_INFERENCE_REQUEST:
            await self._handle_inference_request(msg)
        elif msg_type:
            print(f"[ws_tunnel] 收到消息 type={msg_type}")

    async def _handle_inference_request(self, msg: Dict[str, Any]) -> None:
        """处理云端推理请求：空闲则转发本地 FastAPI，非空闲回 503。"""
        request_id = msg.get("request_id", "unknown")
        payload = msg.get("payload", {})
        monitor = get_yield_monitor()

        if not monitor.is_idle:
            print(f"[ws_tunnel] 节点非空闲，拒绝请求 request_id={request_id}")
            await self._send_json({
                "type": TYPE_RESPONSE_ERROR,
                "request_id": request_id,
                "status": 503,
                "error": "node_yielded",
            })
            return

        if requests is None:
            print(f"[ws_tunnel] 缺少 requests 依赖，无法转发推理请求: {_REQUESTS_IMPORT_ERROR}")
            await self._send_json({
                "type": TYPE_RESPONSE_ERROR,
                "request_id": request_id,
                "status": 500,
                "error": "requests_missing",
            })
            return

        print(f"[ws_tunnel] 正在转发推理请求至本地 request_id={request_id}")
        url = f"http://127.0.0.1:{self._cfg.port}/v1/chat/completions"

        try:
            ok, usage = await asyncio.to_thread(self._forward_stream, url, payload, request_id)
        except Exception as exc:  # noqa: BLE001
            print(f"[ws_tunnel] 本地 API 转发失败: {exc}")
            await self._send_json({
                "type": TYPE_RESPONSE_ERROR,
                "request_id": request_id,
                "status": 500,
                "error": f"local_api_error: {exc}",
            })
            return

        if not ok:
            return

        done_frame: Dict[str, Any] = {"type": TYPE_RESPONSE_DONE, "request_id": request_id}
        if usage:
            # 把本地推理的真实 token 用量带给云端：云端据此精确计费，
            # 而不是按字符数估算（缺 usage 时云端自动回退到估算）。
            done_frame["usage"] = usage
            print(
                "[ws_tunnel] 上报真实 token 用量 request_id=%s prompt=%s completion=%s"
                % (request_id, usage.get("prompt_tokens"), usage.get("completion_tokens"))
            )
        await self._send_json(done_frame)
        print(f"[ws_tunnel] 推理请求完成 request_id={request_id}")

    def _forward_stream(
        self, url: str, payload: Dict[str, Any], request_id: str
    ) -> Tuple[bool, Optional[Dict[str, Any]]]:
        """同步实现：把本地 SSE 响应分块回传云端（在工作线程中执行，不阻塞事件循环）。

        返回 ``(是否成功, 本地上报的真实 usage)``。本地推理若带回了 usage
        （OpenAI 规范的结束帧或任意 chunk 的 usage 字段），这里原样透传给云端。
        """
        usage: Optional[Dict[str, Any]] = None
        try:
            with requests.post(
                url,
                json=payload,
                headers={"Content-Type": "application/json"},
                stream=True,
                timeout=60.0,
            ) as resp:
                if resp.status_code != 200:
                    self._send_json_threadsafe({
                        "type": TYPE_RESPONSE_ERROR,
                        "request_id": request_id,
                        "status": resp.status_code,
                        "error": f"local_api_error_{resp.status_code}",
                    })
                    return False, None
                for line in resp.iter_lines(decode_unicode=True):
                    if not line or not line.startswith("data: "):
                        continue
                    data_str = line[6:].strip()
                    if data_str == "[DONE]":
                        break
                    try:
                        chunk = json.loads(data_str)
                    except json.JSONDecodeError:
                        continue
                    chunk_usage = chunk.get("usage")
                    if isinstance(chunk_usage, dict) and chunk_usage:
                        usage = chunk_usage
                    self._send_json_threadsafe({
                        "type": TYPE_RESPONSE_CHUNK,
                        "request_id": request_id,
                        "chunk": chunk,
                    })
        except requests.exceptions.Timeout:
            print(f"[ws_tunnel] 本地 API 超时 request_id={request_id}")
            self._send_json_threadsafe({
                "type": TYPE_RESPONSE_ERROR,
                "request_id": request_id,
                "status": 504,
                "error": "local_api_timeout",
            })
            return False, None
        except Exception as exc:  # noqa: BLE001
            print(f"[ws_tunnel] 本地 API 转发失败: {exc}")
            self._send_json_threadsafe({
                "type": TYPE_RESPONSE_ERROR,
                "request_id": request_id,
                "status": 500,
                "error": f"local_api_error: {exc}",
            })
            return False, None
        return True, usage

    def _send_json_threadsafe(self, data: Dict[str, Any]) -> None:
        """从工作线程发送消息（投递到事件循环线程执行）。"""
        loop = self._loop
        if loop is None or not loop.is_running():
            return
        asyncio.run_coroutine_threadsafe(self._send_json(data), loop)

    async def _send_json(self, data: Dict[str, Any]) -> None:
        """发送 JSON 消息到云端。"""
        if self._ws is None or _is_closed(self._ws):
            return
        try:
            await self._ws.send(json.dumps(data, ensure_ascii=False))
        except Exception as exc:  # noqa: BLE001 - 发送失败不打断主流程
            print(f"[ws_tunnel] 发送消息失败: {exc}")

    def _notify_offline(self, reason: str = "shutdown") -> None:
        """关闭连接前向云端发送 NODE_OFFLINE 通知（失败不阻塞退出）。"""
        loop = self._loop
        if loop is None or not loop.is_running():
            return
        if self._ws is None or _is_closed(self._ws):
            return
        try:
            future = asyncio.run_coroutine_threadsafe(
                self._send_json({
                    "type": TYPE_NODE_OFFLINE,
                    "node_id": self._node_id,
                    "reason": reason,
                    "timestamp": time.time(),
                }),
                loop,
            )
            future.result(timeout=2.0)
            print("[ws_tunnel] 已向云端发送 NODE_OFFLINE 通知")
        except Exception as exc:  # noqa: BLE001
            print(f"[ws_tunnel] NODE_OFFLINE 通知发送失败（忽略）: {exc}")

    def _build_auth_headers(self) -> Dict[str, str]:
        """构建认证请求头。"""
        headers = {"X-Node-ID": self._node_id}
        if self._node_secret:
            headers["X-Node-Secret"] = self._node_secret
        return headers


# ──────────────────────────────────────────────────────────────
# 全局单例
# ──────────────────────────────────────────────────────────────
_tunnel_instance: Optional[WebSocketTunnel] = None
_tunnel_lock = threading.Lock()


def start_ws_tunnel() -> Optional[WebSocketTunnel]:
    """启动全局 WS 隧道单例。"""
    global _tunnel_instance
    with _tunnel_lock:
        if _tunnel_instance is None:
            _tunnel_instance = WebSocketTunnel()
        _tunnel_instance.start()
        return _tunnel_instance


def stop_ws_tunnel() -> None:
    """停止并释放全局 WS 隧道单例。"""
    global _tunnel_instance
    with _tunnel_lock:
        if _tunnel_instance is not None:
            _tunnel_instance.stop()
            _tunnel_instance = None


def is_ws_connected() -> bool:
    """WebSocket 主通道是否在线（HTTP 心跳据此让位）。"""
    return bool(_tunnel_instance and _tunnel_instance.is_connected())


__all__ = [
    "WebSocketTunnel",
    "start_ws_tunnel",
    "stop_ws_tunnel",
    "is_ws_connected",
]
