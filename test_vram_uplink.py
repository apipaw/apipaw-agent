"""阶段五 P3 · 节点硬件显存上上报链路自检。

用法（在仓库根目录执行）：
    ./.venv/Scripts/python.exe test_vram_uplink.py

背景（阶段五 P3 补漏）：
    云端「能力等级统一算力单价」要生效，前提是云端**知道**节点的硬件显存。
    生产主通道是 **WebSocket 隧道**，而隧道帧此前既不上报显存
    （``ws_tunnel._build_frame``）、云端也不提取显存
    （``server._ws_node_meta``）→ 能力等级永远算不出来，统一单价开关打开也是空转。

    本脚本覆盖 Agent 侧的三件事：
      1. ``telemetry.collect_gpu_metrics_cached()`` 带 TTL 缓存（不重复跑 nvidia-smi）；
      2. ``ws_tunnel._build_frame()`` 的帧里**带**硬件显存字段；
      3. 显存字段的语义正确：报的是**硬件总量**（vram_total_gb），
         而 ``max_vram`` 是「当前模型要多少显存」—— 两者**不可混用**。

测试完全离线：不连生产云端，只在本地构造帧。
"""

import os
import sys
import time

# 必须在导入 telemetry / ws_tunnel 之前设置：不连生产云端
os.environ["APIPAW_DISABLE_TELEMETRY"] = "1"

REPO_DIR = os.path.dirname(os.path.abspath(__file__))
if REPO_DIR not in sys.path:
    sys.path.insert(0, REPO_DIR)

RESULTS = []


def _record(name, ok, detail=""):
    RESULTS.append((name, ok, detail))
    print(("[PASS] " if ok else "[FAIL] ") + name + (" | " + detail if detail else ""))


class _FakeMonitor(object):
    """最小 monitor 替身 —— 只提供 ``_build_frame`` 用到的两个属性。"""

    state = type("S", (object,), {"name": "IDLE"})()
    state_label = "空闲"


def main():
    import telemetry
    import ws_tunnel

    # ---- 1. 缓存版采集：同窗口内重复调用只跑一次底层采集 ----
    calls = {"n": 0}
    original = telemetry.collect_gpu_metrics

    def _counting():
        calls["n"] += 1
        return {
            "gpu_temp_c": 51.0,
            "vram_used_gb": 3.0,
            "vram_total_gb": 24.0,
            "vram_free_gb": 21.0,
        }

    telemetry.collect_gpu_metrics = _counting
    try:
        first = telemetry.collect_gpu_metrics_cached()
        second = telemetry.collect_gpu_metrics_cached()
        _record("缓存版采集：TTL 窗口内重复调用只触发一次底层采集",
                calls["n"] == 1, "calls=%d" % calls["n"])
        _record("缓存版采集：返回形状与原函数一致",
                set(first.keys()) == set(second.keys())
                and "vram_total_gb" in first,
                "keys=%s" % sorted(first.keys()))
        _record("缓存版采集：命中缓存时返回同一份数据",
                second["vram_total_gb"] == 24.0,
                "vram_total_gb=%s" % second["vram_total_gb"])
        # TTL 过期后应重新采集
        telemetry.collect_gpu_metrics_cached(max_age_sec=0.0)
        _record("缓存版采集：TTL 过期后重新采集",
                calls["n"] == 2, "calls=%d" % calls["n"])
    finally:
        telemetry.collect_gpu_metrics = original

    # ---- 2. WS 帧带上硬件显存 ----
    tunnel = ws_tunnel.WebSocketTunnel.__new__(ws_tunnel.WebSocketTunnel)
    tunnel._cfg = type("C", (object,), {
        "node_id": "node-p3-test",
        "node_name": "P3 测试节点",
        "hardware_type": "cuda",
        "device": "cuda",
        "max_vram_gb": 4.5,
    })()
    tunnel._total_completed_tokens = 1234

    # 把采集函数换成固定的假数据，避免依赖本机有没有 nvidia-smi
    telemetry.collect_gpu_metrics_cached = lambda: {
        "gpu_temp_c": 51.0,
        "vram_used_gb": 3.0,
        "vram_total_gb": 24.0,
        "vram_free_gb": 21.0,
    }
    ws_tunnel.collect_gpu_metrics_cached = telemetry.collect_gpu_metrics_cached

    frame = tunnel._build_frame("HEARTBEAT", _FakeMonitor())
    _record("WS 帧带 vram_total_gb（硬件显存总量）",
            frame.get("vram_total_gb") == 24.0,
            "vram_total_gb=%s" % frame.get("vram_total_gb"))
    _record("WS 帧带 vram_used_gb / vram_free_gb",
            frame.get("vram_used_gb") == 3.0 and frame.get("vram_free_gb") == 21.0,
            "used=%s free=%s" % (frame.get("vram_used_gb"), frame.get("vram_free_gb")))
    _record("WS 帧带 gpu_temp_c",
            frame.get("gpu_temp_c") == 51.0,
            "gpu_temp_c=%s" % frame.get("gpu_temp_c"))
    _record("WS 帧保留既有字段（type / node_id / device / max_vram / app_ping）",
            frame.get("type") == "HEARTBEAT"
            and frame.get("node_id") == "node-p3-test"
            and frame.get("device") == "cuda"
            and frame.get("max_vram") == 4.5
            and frame.get("app_ping") == 1)
    _record("WS 帧保留 completed_tokens（既有行为不回退）",
            frame.get("completed_tokens") == 1234)

    # ---- 3. 语义边界：max_vram 与硬件显存不是一回事 ----
    _record("max_vram（当前模型所需显存）与 vram_total_gb（硬件总量）分开上报",
            frame.get("max_vram") == 4.5 and frame.get("vram_total_gb") == 24.0,
            "max_vram=%s vram_total_gb=%s"
            % (frame.get("max_vram"), frame.get("vram_total_gb")))

    # ---- 4. 采集失败降级：字段为 None，而不是抛异常 ----
    telemetry.collect_gpu_metrics_cached = lambda: {
        "gpu_temp_c": None, "vram_used_gb": None,
        "vram_total_gb": None, "vram_free_gb": None,
    }
    ws_tunnel.collect_gpu_metrics_cached = telemetry.collect_gpu_metrics_cached
    degraded = tunnel._build_frame("HEARTBEAT", _FakeMonitor())
    _record("显存采集失败时降级为 None（不抛异常，节点照常上报）",
            degraded.get("vram_total_gb") is None
            and degraded.get("node_id") == "node-p3-test",
            "vram_total_gb=%r" % degraded.get("vram_total_gb"))

    failed = [r for r in RESULTS if not r[1]]
    print("\n===== Agent 显存上报自检：%d/%d 通过 ====="
          % (len(RESULTS), len(RESULTS) - len(failed)))
    if failed:
        for name, _ok, detail in failed:
            print("  FAIL: %s | %s" % (name, detail))
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
