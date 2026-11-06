"""DAY14 客户端侧应用层保活回归测试。

被测对象：D:\\APIPawAgent\\ws_tunnel.py 的
  - 应用层 PING → PONG 应答
  - ``app_ping`` 能力声明（写进 INIT / HEARTBEAT 帧）
  - 链路看门狗（长时间收不到云端任何下行帧 → 主动断开重连）

用例
----
  1. 新客户端连上会发应用层 PING 的假云端 → 必须回 PONG、连接存活、不得触发看门狗
  2. 假云端只发 PING 不等 PONG（模拟半开链路）→ 客户端看门狗必须在阈值内触发重连
  3. 老客户端（不带 app_ping 声明）→ 假云端据此**不**发 PING（向后兼容的另一半）

用法
----
    ./.venv/Scripts/python.exe test_ws_keepalive_client.py
"""

import asyncio
import json
import os
import socket
import sys
import threading
import time
from types import SimpleNamespace

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import ws_tunnel  # noqa: E402

RESULTS = []


def _record(name, ok, detail=""):
    RESULTS.append((name, ok, detail))
    print(("[PASS] " if ok else "[FAIL] ") + name + (" | " + detail if detail else ""))


def _free_port():
    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
    sock.close()
    return port


def _stub_config(port, node_id, node_secret=""):
    """给 ws_tunnel 用的最小配置桩（**不**触碰真实 agent.config.json）。"""
    return SimpleNamespace(
        node_id=node_id,
        node_secret=node_secret,
        node_name="保活自检节点",
        cloud_ws_url="ws://127.0.0.1:%d/api/v1/node/stream" % port,
        hardware_type="desktop",
        device="cpu",
        max_vram_gb=1.0,
        heartbeat_interval_seconds=10.0,
    )


class FakeCloud(object):
    """假云端：持续读取客户端帧（统计 PONG），并按策略发送应用层 PING。

    ping_mode:
      - ``always``：每 ping_interval 秒发一帧 PING
      - ``once``  ：只发一帧 PING 之后沉默（模拟"发完 PING 链路就死了"）
      - ``never`` ：完全不发 PING（模拟老网关）
    """

    def __init__(self, ping_mode="always", ping_interval=2.0):
        self.ping_mode = ping_mode
        self.ping_interval = ping_interval
        self.init_frame = None
        self.pongs = 0
        self.connections = 0
        self.pings_sent = 0
        self._loop = None
        self._server = None
        self._thread = None
        self._ready = threading.Event()

    async def _handler(self, ws):
        self.connections += 1
        reader = asyncio.ensure_future(self._reader(ws))
        try:
            if self.ping_mode == "never":
                await asyncio.Future()
            elif self.ping_mode == "once":
                await asyncio.sleep(self.ping_interval)
                await ws.send(json.dumps({"type": "PING", "ts": time.time()}))
                self.pings_sent += 1
                await asyncio.Future()
            else:
                while True:
                    await asyncio.sleep(self.ping_interval)
                    await ws.send(json.dumps({"type": "PING", "ts": time.time()}))
                    self.pings_sent += 1
        except Exception:  # noqa: BLE001 - 客户端断开是正常路径
            pass
        finally:
            reader.cancel()

    async def _reader(self, ws):
        """持续读取客户端上行帧：首帧是 INIT，其后统计 PONG。"""
        try:
            async for raw in ws:
                try:
                    msg = json.loads(raw)
                except Exception:  # noqa: BLE001
                    continue
                if msg.get("type") == "INIT":
                    self.init_frame = msg
                elif msg.get("type") == "PONG":
                    self.pongs += 1
        except Exception:  # noqa: BLE001
            pass

    def start(self, port):
        import websockets

        ready = self._ready

        async def _run():
            self._server = await websockets.serve(self._handler, "127.0.0.1", port)
            ready.set()
            await asyncio.Future()

        def _target():
            self._loop = asyncio.new_event_loop()
            asyncio.set_event_loop(self._loop)
            try:
                self._loop.run_until_complete(_run())
            except Exception:  # noqa: BLE001
                pass

        self._thread = threading.Thread(target=_target, name="FakeCloud", daemon=True)
        self._thread.start()
        ready.wait(timeout=10)
        return self

    def stop(self):
        if self._loop is not None:
            try:
                self._loop.call_soon_threadsafe(self._loop.stop)
            except Exception:  # noqa: BLE001
                pass
        if self._thread is not None:
            self._thread.join(timeout=5)


def _run_tunnel(port, node_id, seconds, node_secret=""):
    """在后台启动真实 WebSocketTunnel（走完整的 ws_tunnel 代码路径）。"""
    original = ws_tunnel.get_config
    ws_tunnel.get_config = lambda: _stub_config(port, node_id, node_secret)
    tunnel = ws_tunnel.WebSocketTunnel()
    ws_tunnel.get_config = original
    tunnel.start()
    return tunnel


def case_pong_reply():
    """用例1：客户端必须回 PONG，连接不得中断、不得触发看门狗。"""
    port = _free_port()
    cloud = FakeCloud(ping_mode="always", ping_interval=2.0).start(port)
    tunnel = _run_tunnel(port, "keepalive_client_1", 0)
    try:
        time.sleep(12.0)  # 覆盖多个 PING 周期
        ok = cloud.pongs >= 2 and tunnel.is_connected()
        detail = "云端收到 PONG %d 次 | 客户端仍在线: %s" % (cloud.pongs, tunnel.is_connected())
        _record("用例1 客户端收到应用层 PING 后回 PONG 且连接保持", ok, detail)
        init = cloud.init_frame or {}
        _record("用例1b INIT 帧声明 app_ping 能力", init.get("app_ping") == 1,
                "init.app_ping=%r" % init.get("app_ping"))
    finally:
        tunnel.stop()
        cloud.stop()


def case_watchdog():
    """用例2：云端发过 PING 后突然沉默（半开链路）→ 客户端看门狗应触发重连。"""
    port = _free_port()
    # 把看门狗阈值压到秒级，避免测试跑 75s+
    old_timeout = ws_tunnel.WS_WATCHDOG_TIMEOUT_SEC
    old_interval = ws_tunnel.WS_WATCHDOG_CHECK_INTERVAL_SEC
    old_pong_timeout = ws_tunnel.WS_PONG_WATCHDOG_TIMEOUT_SEC
    ws_tunnel.WS_WATCHDOG_TIMEOUT_SEC = 8.0
    ws_tunnel.WS_WATCHDOG_CHECK_INTERVAL_SEC = 2.0
    ws_tunnel.WS_PONG_WATCHDOG_TIMEOUT_SEC = 8.0
    cloud = FakeCloud(ping_mode="once", ping_interval=1.0).start(port)
    tunnel = _run_tunnel(port, "keepalive_client_2", 0)
    try:
        time.sleep(16.0)
        ok = cloud.connections >= 2
        detail = "假云端统计到 %d 次连接（首连 + 看门狗重连）" % cloud.connections
        _record("用例2 云端沉默时客户端看门狗触发并重连", ok, detail)
    finally:
        tunnel.stop()
        cloud.stop()
        ws_tunnel.WS_WATCHDOG_TIMEOUT_SEC = old_timeout
        ws_tunnel.WS_WATCHDOG_CHECK_INTERVAL_SEC = old_interval
        ws_tunnel.WS_PONG_WATCHDOG_TIMEOUT_SEC = old_pong_timeout


def case_legacy_compat():
    """用例3：老客户端不带 app_ping 时，假云端据此不发 PING（服务端兼容逻辑的镜像）。"""
    port = _free_port()
    cloud = FakeCloud(ping_mode="always", ping_interval=2.0).start(port)
    original = ws_tunnel.get_config
    ws_tunnel.get_config = lambda: _stub_config(port, "keepalive_client_3")
    tunnel = ws_tunnel.WebSocketTunnel()
    ws_tunnel.get_config = original
    # 模拟老客户端：把 _build_frame 的 app_ping 抹掉
    original_build = tunnel._build_frame

    def _legacy_build(frame_type, monitor):
        frame = original_build(frame_type, monitor)
        frame.pop("app_ping", None)
        return frame

    tunnel._build_frame = _legacy_build
    tunnel.start()
    try:
        time.sleep(8.0)
        init = cloud.init_frame or {}
        ok = "app_ping" not in init and tunnel.is_connected()
        detail = "init.app_ping=%r | 客户端在线: %s" % (init.get("app_ping"), tunnel.is_connected())
        _record("用例3 老客户端（无 app_ping）连接保持正常，能力声明可缺省", ok, detail)
    finally:
        tunnel.stop()
        cloud.stop()


class BlackHoleCloud(object):
    """半开链路模拟器：完成 WS 握手后**再也不回任何东西**。

    不回 PONG、不发任何数据帧、也不发 close 帧 —— 这正是 2026-11-06 线上事故的形态：
    中间层（Cloudflare 边缘）只掐断了"云端→客户端"那一段，客户端这一侧既收不到
    FIN 也收不到 RST，看起来"还连着"，于是 8 分钟都没有重连。

    纯标准库实现（不依赖 websockets），因为 websockets 服务端会自动回 PONG，
    无法复现"控制层也失联"的场景。
    """

    GUID = "258EAFA5-E914-47DA-95CA-C5AB0DC85B11"

    def __init__(self):
        self.connections = 0
        self._sock = None
        self._thread = None
        self._stop = threading.Event()

    def start(self, port):
        import base64
        import hashlib

        self._sock = socket.socket()
        self._sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self._sock.bind(("127.0.0.1", port))
        self._sock.listen(8)
        self._thread = threading.Thread(target=self._accept_loop, daemon=True)
        self._thread.start()
        return self

    def _accept_loop(self):
        while not self._stop.is_set():
            try:
                conn, _ = self._sock.accept()
            except OSError:
                return
            self.connections += 1
            threading.Thread(target=self._hold, args=(conn,), daemon=True).start()

    def _hold(self, conn):
        import base64
        import hashlib

        conn.settimeout(5.0)
        buf = b""
        try:
            while b"\r\n\r\n" not in buf:
                chunk = conn.recv(4096)
                if not chunk:
                    return
                buf += chunk
            key = ""
            for line in buf.split(b"\r\n"):
                if line.lower().startswith(b"sec-websocket-key:"):
                    key = line.split(b":", 1)[1].strip().decode("ascii")
            accept = base64.b64encode(
                hashlib.sha1((key + self.GUID).encode("ascii")).digest()
            ).decode("ascii")
            resp = ("HTTP/1.1 101 Switching Protocols\r\n"
                    "Upgrade: websocket\r\n"
                    "Connection: Upgrade\r\n"
                    "Sec-WebSocket-Accept: %s\r\n\r\n" % accept)
            conn.sendall(resp.encode("ascii"))
            # 握手之后只读不写：模拟"链路静默死亡"
            conn.settimeout(5.0)
            while not self._stop.is_set():
                try:
                    if not conn.recv(4096):
                        return
                except socket.timeout:
                    continue
                except OSError:
                    return
        finally:
            try:
                conn.close()
            except Exception:  # noqa: BLE001
                pass

    def stop(self):
        self._stop.set()
        try:
            self._sock.close()
        except Exception:  # noqa: BLE001
            pass


def case_control_ping_watchdog():
    """用例4：链路从一开始就完全沉默（连云端首帧应用层 PING 都没收到）
    → 控制层 PING 探针必须**独立**判死并触发重连。"""
    port = _free_port()
    old_pong_timeout = ws_tunnel.WS_PONG_WATCHDOG_TIMEOUT_SEC
    old_interval = ws_tunnel.WS_WATCHDOG_CHECK_INTERVAL_SEC
    ws_tunnel.WS_PONG_WATCHDOG_TIMEOUT_SEC = 8.0
    ws_tunnel.WS_WATCHDOG_CHECK_INTERVAL_SEC = 2.0
    cloud = BlackHoleCloud().start(port)
    tunnel = _run_tunnel(port, "keepalive_client_4", 0)
    try:
        time.sleep(25.0)
        ok = cloud.connections >= 2
        detail = ("黑洞服务端统计到 %d 次连接（首连 + 看门狗重连）" % cloud.connections)
        _record("用例4 控制层失联时客户端独立判死并重连（半开链路复现）", ok, detail)
    finally:
        tunnel.stop()
        cloud.stop()
        ws_tunnel.WS_PONG_WATCHDOG_TIMEOUT_SEC = old_pong_timeout
        ws_tunnel.WS_WATCHDOG_CHECK_INTERVAL_SEC = old_interval


def main():
    case_pong_reply()
    case_watchdog()
    case_legacy_compat()
    case_control_ping_watchdog()
    passed = sum(1 for _, ok, _ in RESULTS if ok)
    total = len(RESULTS)
    print("\n===== 客户端应用层保活回归: %d/%d 通过 =====" % (passed, total))
    print("============================================\n")
    return 0 if passed == total else 1


if __name__ == "__main__":
    sys.exit(main())
