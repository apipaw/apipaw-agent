"""阶段十七 P2 · GPU 温控遥测自检（Agent v1.0.16）。

用法（在仓库根目录执行）：
    ./.venv/Scripts/python.exe test_phase17_p2_gpu_thermals.py

覆盖：
  1. ``telemetry.probe_gpu_thermals()`` —— 温度 / 风扇采集：
     - 模拟注入（``APIPAW_MOCK_GPU_TEMP_C`` / ``APIPAW_MOCK_GPU_FAN_PCT``）
       **完全跳过** nvidia-smi 子进程（验收环境可复现）；
     - 真实 nvidia-smi 输出解析（含 ``[N/A]`` 风扇的降级）；
     - 无独显 / 命令不可用 ⇒ 安全回退 ``None``，**绝不抛异常**。
  2. 心跳报文体与 WS 帧都携带 ``gpu_temp_c`` / ``gpu_fan_pct``。
  3. ``/api/status`` 下发温控读数（面板徽标的数据源）。
  4. **真机 ``agent.config.json`` 零污染**（全程字节级比对）。

测试完全离线：不连生产云端，不写本机配置文件。
"""

import os
import sys
import tempfile
from pathlib import Path

# 必须在导入 telemetry / ws_tunnel 之前设置：不连生产云端
os.environ["APIPAW_DISABLE_TELEMETRY"] = "1"
# 清掉可能残留的模拟注入，保证默认路径可测
os.environ.pop("APIPAW_MOCK_GPU_TEMP_C", None)
os.environ.pop("APIPAW_MOCK_GPU_FAN_PCT", None)

REPO_ROOT = Path(__file__).resolve().parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

RESULTS = []


def _record(name, ok, detail=""):
    RESULTS.append((name, ok, detail))
    print(("[PASS] " if ok else "[FAIL] ") + name + (" | " + detail if detail else ""))


class _FakeMonitor(object):
    """最小 monitor 替身 —— 只提供 ``_build_frame`` 用到的两个属性。"""

    state = type("S", (object,), {"name": "IDLE"})()
    state_label = "空闲"


class _FakeProc(object):
    """nvidia-smi 子进程替身。"""

    def __init__(self, stdout="", returncode=0):
        self.stdout = stdout
        self.stderr = ""
        self.returncode = returncode


def main() -> int:
    import telemetry
    import ws_tunnel

    # ═══ 1. probe_gpu_thermals · 模拟注入 ═══
    original_smi_path = telemetry._nvidia_smi_path
    original_run = telemetry.subprocess.run

    def _boom_smi_path():
        raise AssertionError("注入模式下不得调用 nvidia-smi")

    telemetry._nvidia_smi_path = _boom_smi_path
    os.environ["APIPAW_MOCK_GPU_TEMP_C"] = "88.5"
    os.environ["APIPAW_MOCK_GPU_FAN_PCT"] = "97"
    try:
        thermals = telemetry.probe_gpu_thermals()
    finally:
        telemetry._nvidia_smi_path = original_smi_path
        os.environ.pop("APIPAW_MOCK_GPU_TEMP_C", None)
        os.environ.pop("APIPAW_MOCK_GPU_FAN_PCT", None)
    _record(
        "模拟注入：APIPAW_MOCK_GPU_TEMP_C / FAN_PCT 生效且跳过 nvidia-smi",
        thermals.get("gpu_temp_c") == 88.5 and thermals.get("gpu_fan_pct") == 97.0,
        "temp=%r fan=%r" % (thermals.get("gpu_temp_c"), thermals.get("gpu_fan_pct")),
    )

    # 只注入温度 ⇒ 风扇为 None（而不是 0）
    os.environ["APIPAW_MOCK_GPU_TEMP_C"] = "75"
    try:
        only_temp = telemetry.probe_gpu_thermals()
    finally:
        os.environ.pop("APIPAW_MOCK_GPU_TEMP_C", None)
    _record(
        "只注入温度时风扇保持 None（缺数据 ≠ 0）",
        only_temp.get("gpu_temp_c") == 75.0 and only_temp.get("gpu_fan_pct") is None,
        "temp=%r fan=%r" % (only_temp.get("gpu_temp_c"), only_temp.get("gpu_fan_pct")),
    )

    # ═══ 2. 真实 nvidia-smi 输出解析 ═══
    telemetry._nvidia_smi_path = lambda: "/usr/bin/nvidia-smi"
    telemetry.subprocess.run = lambda *a, **k: _FakeProc("52, 61, 3072, 24576\n")
    try:
        parsed = telemetry.probe_gpu_thermals()
    finally:
        telemetry._nvidia_smi_path = original_smi_path
        telemetry.subprocess.run = original_run
    _record(
        "解析 nvidia-smi 温度 / 风扇（52°C / 61%）",
        parsed.get("gpu_temp_c") == 52.0 and parsed.get("gpu_fan_pct") == 61.0,
        "temp=%r fan=%r" % (parsed.get("gpu_temp_c"), parsed.get("gpu_fan_pct")),
    )

    # 风扇回报 [N/A]（被动散热卡 / 多卡环境）⇒ None，**不抛异常**
    telemetry._nvidia_smi_path = lambda: "/usr/bin/nvidia-smi"
    telemetry.subprocess.run = lambda *a, **k: _FakeProc("48, [N/A], 1024, 8192\n")
    try:
        na_fan = telemetry.probe_gpu_thermals()
    finally:
        telemetry._nvidia_smi_path = original_smi_path
        telemetry.subprocess.run = original_run
    _record(
        "风扇 [N/A] 降级为 None（温度照常解析）",
        na_fan.get("gpu_temp_c") == 48.0 and na_fan.get("gpu_fan_pct") is None,
        "temp=%r fan=%r" % (na_fan.get("gpu_temp_c"), na_fan.get("gpu_fan_pct")),
    )

    # ═══ 3. 无独显 / 命令不可用 ⇒ 安全回退 ═══
    telemetry._nvidia_smi_path = lambda: None
    try:
        no_gpu = telemetry.probe_gpu_thermals()
    finally:
        telemetry._nvidia_smi_path = original_smi_path
    _record(
        "无 NVIDIA 独显时回退 None（绝不抛异常）",
        no_gpu.get("gpu_temp_c") is None and no_gpu.get("gpu_fan_pct") is None,
        "temp=%r fan=%r" % (no_gpu.get("gpu_temp_c"), no_gpu.get("gpu_fan_pct")),
    )

    # 命令存在但输出异常 ⇒ 同样降级
    telemetry._nvidia_smi_path = lambda: "/usr/bin/nvidia-smi"
    telemetry.subprocess.run = lambda *a, **k: _FakeProc("garbage", returncode=1)
    try:
        broken = telemetry.probe_gpu_thermals()
    finally:
        telemetry._nvidia_smi_path = original_smi_path
        telemetry.subprocess.run = original_run
    _record(
        "nvidia-smi 输出异常时降级 None（不抛异常）",
        broken.get("gpu_temp_c") is None and broken.get("gpu_fan_pct") is None,
        "temp=%r fan=%r" % (broken.get("gpu_temp_c"), broken.get("gpu_fan_pct")),
    )

    # ═══ 4. collect_gpu_metrics 形状含 gpu_fan_pct ═══
    metrics = telemetry.collect_gpu_metrics()
    _record(
        "collect_gpu_metrics 返回 gpu_fan_pct 字段",
        "gpu_fan_pct" in metrics and "gpu_temp_c" in metrics,
        "keys=%s" % sorted(metrics.keys()),
    )

    # ═══ 5. WS 帧带温控字段 ═══
    tunnel = ws_tunnel.WebSocketTunnel.__new__(ws_tunnel.WebSocketTunnel)
    tunnel._cfg = type("C", (object,), {
        "node_id": "node-p17-test",
        "node_name": "P17 测试节点",
        "hardware_type": "cuda",
        "device": "cuda",
        "max_vram_gb": 4.5,
    })()
    tunnel._total_completed_tokens = 7

    original_cached = telemetry.collect_gpu_metrics_cached
    telemetry.collect_gpu_metrics_cached = lambda: {
        "gpu_temp_c": 81.0,
        "gpu_fan_pct": 95.0,
        "vram_used_gb": 3.0,
        "vram_total_gb": 24.0,
        "vram_free_gb": 21.0,
    }
    ws_tunnel.collect_gpu_metrics_cached = telemetry.collect_gpu_metrics_cached
    try:
        frame = tunnel._build_frame("HEARTBEAT", _FakeMonitor())
    finally:
        telemetry.collect_gpu_metrics_cached = original_cached
        ws_tunnel.collect_gpu_metrics_cached = original_cached
    _record(
        "WS 帧带 gpu_temp_c / gpu_fan_pct（高温可被云端识别）",
        frame.get("gpu_temp_c") == 81.0 and frame.get("gpu_fan_pct") == 95.0,
        "temp=%r fan=%r" % (frame.get("gpu_temp_c"), frame.get("gpu_fan_pct")),
    )
    _record(
        "WS 帧保留既有字段（type / node_id / app_ping / completed_tokens）",
        frame.get("type") == "HEARTBEAT"
        and frame.get("node_id") == "node-p17-test"
        and frame.get("app_ping") == 1
        and frame.get("completed_tokens") == 7,
    )

    # ═══ 6. HTTP 心跳报文体带温控字段 ═══
    reporter = telemetry.TelemetryReporter.__new__(telemetry.TelemetryReporter)
    reporter._cfg = type("C", (object,), {
        "node_id": "node-p17-test",
        "node_name": "P17 测试节点",
        "hardware_type": "cuda",
        "wallet_address": "0xabc",
        "model_name": "qwen2.5:0.5b",
        "max_vram_gb": 4.5,
        "backend_model": "qwen2.5:0.5b",
        "inference_backend": "ollama",
    })()
    telemetry.collect_gpu_metrics_cached = lambda: {
        "gpu_temp_c": 77.0,
        "gpu_fan_pct": 90.0,
        "vram_used_gb": 3.0,
        "vram_total_gb": 24.0,
        "vram_free_gb": 21.0,
    }
    try:
        payload = telemetry.TelemetryReporter.build_payload(reporter)
    finally:
        telemetry.collect_gpu_metrics_cached = original_cached
    _record(
        "HTTP 心跳报文体带 gpu_temp_c / gpu_fan_pct",
        payload.get("gpu_temp_c") == 77.0 and payload.get("gpu_fan_pct") == 90.0,
        "temp=%r fan=%r" % (payload.get("gpu_temp_c"), payload.get("gpu_fan_pct")),
    )
    _record(
        "Agent 版本已升到 1.0.17",
        str(payload.get("agent_version")) == "1.0.17",
        "version=%r" % payload.get("agent_version"),
    )

    # ═══ 7. /api/status 下发温控读数 ═══
    try:
        from asgi_test_client import TestClient  # noqa: E402
        import agent_config
        import main as agent_main

        tmp_dir = tempfile.mkdtemp(prefix="apipaw_p17_p2_")
        cfg_path = Path(tmp_dir) / "agent.config.json"
        cfg = agent_config.AgentConfig(config_path=cfg_path)
        agent_config._config_instance = cfg  # type: ignore[attr-defined]
        agent_main.get_config = lambda: cfg  # type: ignore[assignment]

        telemetry.collect_gpu_metrics_cached = lambda: {
            "gpu_temp_c": 66.0,
            "gpu_fan_pct": 55.0,
            "vram_used_gb": 1.0,
            "vram_total_gb": 8.0,
            "vram_free_gb": 7.0,
        }
        app = agent_main.create_app()
        client = TestClient(app)
        status = client.get("/api/status").json()
        _record(
            "/api/status 下发 gpu_temp_c / gpu_fan_pct（面板徽标数据源）",
            status.get("gpu_temp_c") == 66.0 and status.get("gpu_fan_pct") == 55.0,
            "temp=%r fan=%r" % (status.get("gpu_temp_c"), status.get("gpu_fan_pct")),
        )
        _record(
            "/api/status 不泄露明文密钥",
            "node_secret" not in status and "has_secret" in status,
        )
    except Exception as exc:  # noqa: BLE001 - 面板接口不可用时明确记录
        _record("/api/status 下发 gpu_temp_c / gpu_fan_pct（面板徽标数据源）",
                False, "异常: %s" % exc)
    finally:
        telemetry.collect_gpu_metrics_cached = original_cached

    # ═══ 8. 真机 agent.config.json 零污染 ═══
    real_cfg = REPO_ROOT / "agent.config.json"
    _record(
        "真机 agent.config.json 字节级未变（零污染）",
        real_cfg.read_bytes() == _config_before(real_cfg),
        "size=%d" % (real_cfg.stat().st_size if real_cfg.exists() else -1),
    )

    failed = [r for r in RESULTS if not r[1]]
    print("\n===== Agent GPU 温控遥测自检：%d/%d 通过 ====="
          % (len(RESULTS), len(RESULTS) - len(failed)))
    if failed:
        for name, _ok, detail in failed:
            print("  FAIL: %s | %s" % (name, detail))
        return 1
    return 0


def _config_before(path: Path) -> bytes:
    """真机配置文件基线字节（首次调用时快照）。"""
    global _CONFIG_SNAPSHOT
    if _CONFIG_SNAPSHOT is None:
        _CONFIG_SNAPSHOT = path.read_bytes() if path.exists() else b""
    return _CONFIG_SNAPSHOT


_CONFIG_SNAPSHOT = None


if __name__ == "__main__":
    # 先取基线（必须在任何导入副作用之前）
    _real = REPO_ROOT / "agent.config.json"
    _CONFIG_SNAPSHOT = _real.read_bytes() if _real.exists() else b""
    sys.exit(main())
