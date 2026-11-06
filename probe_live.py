"""DAY14 阶段二：真实 Agent 隧道直连生产云端，长连接压力观测（前台可控输出）。

用法：
    ./.venv/Scripts/python.exe -u probe_live.py --seconds 260

它启动**真实的** ws_tunnel.WebSocketTunnel（读 agent.config.json，连 wss://cloud.apipaw.com），
并对关键路径做"旁路打点"（不改动被测代码），用来判定：
  - 连接是否连续存活超过 200s（跨过两个 90s 死亡窗口）
  - 是否出现 1006 / 1011 异常断开
  - 云端的应用层 PING 是否真的在发、客户端是否回了 PONG
"""

import argparse
import json
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import ws_tunnel  # noqa: E402

START = time.time()
EVENTS = []


def log(msg):
    print("[%7.1fs] %s" % (time.time() - START, msg), flush=True)


def _event(kind, detail=""):
    EVENTS.append((round(time.time() - START, 1), kind, detail))
    log("%s %s" % (kind, detail))


def _instrument(tunnel):
    """给真实隧道挂上旁路打点。"""
    orig_init = tunnel._send_init
    orig_handle = tunnel._handle_message

    async def send_init():
        await orig_init()
        _event("[CONNECT]", "INIT 已发送")

    async def handle_message(raw_msg):
        try:
            msg = json.loads(raw_msg) if isinstance(raw_msg, (str, bytes)) else raw_msg
            mtype = msg.get("type") if isinstance(msg, dict) else None
        except Exception:  # noqa: BLE001
            mtype = None
        if mtype in ("PING", "PONG"):
            _event("[DOWN %s]" % mtype, "")
        await orig_handle(raw_msg)

    tunnel._send_init = send_init
    tunnel._handle_message = handle_message


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--seconds", type=int, default=260)
    ap.add_argument("--url", default="",
                    help="覆盖 agent.config.json 里的云端地址（用于直连源站）")
    args = ap.parse_args()

    if args.url:
        cfg = ws_tunnel.get_config()
        cfg._model.cloud_ws_url = args.url
        ws_tunnel.get_config = lambda: cfg
        log("覆盖云端地址: %s" % args.url)

    tunnel = ws_tunnel.WebSocketTunnel()
    _instrument(tunnel)
    tunnel.start()
    log("隧道已启动，观测 %ds" % args.seconds)

    deadline = time.time() + args.seconds
    while time.time() < deadline:
        time.sleep(2.0)
    alive = tunnel.is_connected()
    log("观测结束：仍在线=%s" % alive)
    tunnel.stop()
    time.sleep(1.0)

    connects = [e for e in EVENTS if e[1] == "[CONNECT]"]
    print("\n===== 长连接观测汇总 =====")
    print("观测时长: %ds | 结束状态在线: %s" % (args.seconds, alive))
    print("连接次数: %d（1 = 全程未断）" % len(connects))
    print("下行应用层 PING 次数: %d | PONG 次数: %d"
          % (sum(1 for e in EVENTS if e[1] == "[DOWN PING]"),
             sum(1 for e in EVENTS if e[1] == "[DOWN PONG]")))
    print("事件时间线:")
    for t, kind, detail in EVENTS:
        print("   %8.1fs  %s %s" % (t, kind, detail))
    print("==============================\n")
    return 0 if len(connects) == 1 and alive else 1


if __name__ == "__main__":
    sys.exit(main())
