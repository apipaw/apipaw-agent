"""
yield_smoke_test.py —— CI 让退机制冒烟测试

被 `.github/workflows/ci.yml` 调用，也可在本地手动执行：
    python .github/scripts/yield_smoke_test.py

覆盖：
1. 配置读写往返（独立子进程 + 临时文件，**不触碰仓库内的 agent.config.json**）
2. 让退状态机迁移：IDLE -> BUSY -> IDLE -> YIELDED -> PAUSED -> IDLE
3. 让退指标结构与 SLA 计数
4. 控制台编码兜底（GBK 下 emoji 不得抛 UnicodeEncodeError）

本脚本不依赖 torch / pystray，用于验证「零重依赖降级」承诺。
注意：这里 **不设置** ``APIPAW_FORCE_IDLE``，否则让退状态机会被钉死在 IDLE。
"""

from __future__ import annotations

import os
import subprocess
import sys
import time
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT))

from console import enable_utf8_output  # noqa: E402
from yield_monitor import NodeState, YieldMonitor, get_yield_monitor  # noqa: E402


def check_config_roundtrip() -> None:
    # AgentConfig 是进程级单例，配置往返必须放在独立子进程中，
    # 否则会污染后续用例使用的全局配置实例。
    code = (
        "import json, sys, tempfile\n"
        "from pathlib import Path\n"
        "sys.path.insert(0, %r)\n"
        "from agent_config import AgentConfig\n"
        "with tempfile.TemporaryDirectory() as tmp:\n"
        "    path = Path(tmp) / 'agent.config.json'\n"
        "    cfg = AgentConfig(config_path=path)\n"
        "    assert path.exists(), 'default config not created'\n"
        "    cfg.update(node_name='ci-node', hardware_type='laptop', wallet_address='0xCI')\n"
        "    cfg.reload()\n"
        "    assert cfg.node_name == 'ci-node', cfg.node_name\n"
        "    assert cfg.hardware_type == 'laptop', cfg.hardware_type\n"
        "    assert cfg.wallet_address == '0xCI', cfg.wallet_address\n"
        "    raw = json.loads(path.read_text(encoding='utf-8'))\n"
        "    assert raw['node_name'] == 'ci-node', raw['node_name']\n"
        "    assert raw['port'] == 52415, raw['port']\n"
        "print('config-ok')\n" % str(REPO_ROOT)
    )
    proc = subprocess.run(
        [sys.executable, "-c", code],
        capture_output=True,
        text=True,
        timeout=120,
    )
    assert proc.returncode == 0, f"配置往返失败:\n{proc.stdout}\n{proc.stderr}"
    assert "config-ok" in proc.stdout, f"配置往返未输出成功标记:\n{proc.stdout}"
    print("[OK] 配置写入 / 重载 / 磁盘一致性")


def check_console_guard() -> None:
    enable_utf8_output()
    for stream in (sys.stdout, sys.stderr):
        errors = getattr(stream, "errors", None)
        if errors is not None:
            assert errors == "replace", f"{stream.name} 未启用编码兜底 (errors={errors})"
    print("[OK] 控制台编码兜底已启用")


def check_state_machine() -> None:
    # 使用独立实例且 **不启动轮询线程**，避免宿主环境的真实输入事件干扰断言
    monitor = YieldMonitor()

    assert monitor.state is NodeState.IDLE, f"初始状态应为 IDLE，实际 {monitor.state}"

    assert monitor.set_busy() is True, "IDLE 状态下应能进入 BUSY"
    assert monitor.state is NodeState.BUSY, f"状态应为 BUSY，实际 {monitor.state}"
    monitor.set_idle()
    assert monitor.state is NodeState.IDLE, f"状态应回到 IDLE，实际 {monitor.state}"

    monitor.yield_now("ci-smoke")
    assert monitor.is_yielded, "yield_now 后未进入 YIELDED"

    monitor.pause()
    assert monitor.is_paused, "pause 失败"
    monitor.resume()
    assert monitor.state is NodeState.IDLE, f"resume 后状态应为 IDLE，实际 {monitor.state}"

    metrics = monitor.metrics()
    for key in ("state", "yield_count", "sla_ms", "sla_violations", "last_yield_latency_ms"):
        assert key in metrics, f"metrics 缺少字段 {key}"
    assert metrics["yield_count"] >= 1, "yield_count 未累加"
    assert metrics["sla_ms"] == 50, f"SLA 应为 50ms，实际 {metrics['sla_ms']}"
    assert metrics["sla_violations"] == 0, f"SLA 违约数异常: {metrics['sla_violations']}"
    print(
        f"[OK] 状态机迁移 OK | yield_count={metrics['yield_count']} "
        f"| latency={metrics['last_yield_latency_ms']}ms | sla={metrics['sla_ms']}ms"
    )


def check_monitor_thread() -> None:
    monitor = get_yield_monitor()
    monitor.start()
    time.sleep(0.3)
    try:
        metrics = monitor.metrics()
        assert metrics["poll_interval_ms"] == 5, f"轮询间隔应为 5ms，实际 {metrics['poll_interval_ms']}"
        assert metrics["timer_precision_1ms"] is True, "计时器精度未提升到 1ms"
        print("[OK] 轮询线程启动 OK | 5ms 轮询 | 1ms 计时器精度")
    finally:
        monitor.stop()


def main() -> int:
    check_config_roundtrip()
    check_console_guard()
    check_state_machine()
    check_monitor_thread()
    print("\nALL YIELD SMOKE TESTS PASSED")
    return 0


if __name__ == "__main__":
    sys.exit(main())
