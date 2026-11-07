"""合并后热重载功能的沙箱验证（不触碰真实配置、不连真实云端）。

场景：
  1. 假 WS 服务端在 X-Node-Secret 错误时返回 403（复刻云端逐节点鉴权）
  2. 隧道用错误密钥启动 → 被反复拒绝
  3. 运行时更新配置为正确密钥 → 调用 reload_credentials() → 应在 1s 内重连成功
  4. 同时验证 DAY14 的看门狗/PONG 能力没有被这次合并破坏

全程在临时目录里跑副本，真实 agent.config.json 不受影响。
"""

import asyncio
import json
import os
import shutil
import socket
import sys
import tempfile
import time

REPO = os.path.dirname(os.path.abspath(__file__))
MODULES = ["agent_config.py", "console.py", "telemetry.py", "token_counter.py",
           "tray_icon.py", "web_ui.py", "ws_tunnel.py", "yield_monitor.py"]
GOOD_SECRET = "good-secret-xyz"
BAD_SECRET = "bad-secret-000"


def free_port():
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    p = s.getsockname()[1]
    s.close()
    return p


async def fake_server(port, stop_flag):
    """假云端：密钥不对就 403 拒绝握手；对则接受并回声。"""
    from websockets.asyncio.server import serve

    async def handler(ws):
        # 保持连接存活（直到客户端关闭），不要读完一帧就断开，
        # 否则节点刚连上就被服务端关掉，测不出"热重载后已连上"。
        try:
            await ws.wait_closed()
        except Exception:
            pass

    def process_request(connection, request):
        secret = request.headers.get("X-Node-Secret")
        if secret != GOOD_SECRET:
            return connection.respond(403, "Forbidden")
        return None

    async with serve(handler, "127.0.0.1", port, process_request=process_request):
        while not stop_flag.is_set():
            await asyncio.sleep(0.1)


async def main():
    tmp = tempfile.mkdtemp(prefix="apipaw_ui_merge_")
    for name in MODULES:
        shutil.copy(os.path.join(REPO, name), os.path.join(tmp, name))
    sys.path.insert(0, tmp)
    os.chdir(tmp)

    port = free_port()
    cfg_data = {
        "host": "127.0.0.1", "port": 52499,
        "api_base_url": "http://127.0.0.1:52499/v1",
        "model_name": "apipaw-gpt2-small",
        "node_id": "node_merge_test", "node_name": "merge-test",
        "hardware_type": "desktop", "wallet_address": "",
        "node_secret": BAD_SECRET, "device": "cpu", "max_vram_gb": 1.0,
        "idle_threshold_seconds": 15.0, "vram_yield_ms": 50, "poll_interval_seconds": 5,
        "cloud_api_base": "http://127.0.0.1:%d" % port,
        "cloud_heartbeat_url": "http://127.0.0.1:%d/api/v1/nodes/heartbeat" % port,
        "cloud_ws_url": "ws://127.0.0.1:%d/api/v1/node/stream" % port,
        "heartbeat_interval_seconds": 10.0, "disable_telemetry": False,
        "enable_tray": False, "log_level": "INFO",
    }
    with open(os.path.join(tmp, "agent.config.json"), "w", encoding="utf-8") as fh:
        json.dump(cfg_data, fh, ensure_ascii=False, indent=4)

    import ws_tunnel as wt

    stop_flag = asyncio.Event()
    server_task = asyncio.create_task(fake_server(port, stop_flag))
    await asyncio.sleep(0.6)

    tunnel = wt.start_ws_tunnel()
    print("[test] 隧道已启动（配置为错误密钥）")

    await asyncio.sleep(3.0)
    assert not tunnel.is_connected(), "错误密钥不应连上"
    print("[PASS] 错误密钥被假云端 403 拒绝，未建立连接")

    # 运行时热更新为正确密钥
    cfg = wt.get_config()
    cfg.update(node_secret=GOOD_SECRET)
    print("[test] 已更新配置为正确密钥，调用 reload_credentials() ...")
    started = time.time()
    result = await asyncio.get_running_loop().run_in_executor(
        None, wt.reload_tunnel_credentials, 10.0)
    elapsed = time.time() - started
    print("[test] reload 结果: %s（耗时 %.2fs）" % (result, elapsed))

    ok = bool(result.get("connected")) and tunnel.is_connected()
    print("[PASS] 热重载后隧道已连接" if ok else "[FAIL] 热重载后未连接")
    print("[PASS] 热重载耗时 %.2fs（未干等退避计时）" % elapsed if elapsed < 3.0
          else "[WARN] 热重载耗时 %.2fs，可能仍在等退避" % elapsed)

    headers = tunnel._build_auth_headers()
    ok_hdr = headers.get("X-Node-Secret") == GOOD_SECRET and headers.get("X-Node-ID") == "node_merge_test"
    print("[PASS] 握手头实时读取新配置" if ok_hdr else "[FAIL] 握手头未刷新: %s" % headers)

    has_watchdog = hasattr(tunnel, "_watchdog_loop")
    print("[PASS] DAY14 链路看门狗能力保留" if has_watchdog else "[FAIL] 看门狗丢失")

    wt.stop_ws_tunnel()
    stop_flag.set()
    server_task.cancel()
    shutil.rmtree(tmp, ignore_errors=True)
    return 0 if ok and ok_hdr and has_watchdog else 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
