"""
test_phase20_p0_mesh_federation.py —— 阶段二十 P0/P1/P2 · Agent 侧自检

覆盖三块：

1. **P0 Pipeline Mesh**（``mesh_cluster.py``）
   - 2×6GB ⇒ 12GB 集群、80 层切 ``L0-39`` / ``L40-79``；
   - RTT ≥ 25ms 的 peer **被剔除**（fail closed）；
   - 显存未知的 peer **被剔除**（不把「未知」当「有」）；
   - 不可达的 peer **被剔除**；
   - 环境变量注入的模拟拓扑可复现（验收可在无局域网环境跑）。
2. **P1 macOS / Apple Silicon**（``yield_monitor.py`` / ``telemetry.py``）
   - ``ioreg`` 的 ``HIDIdleTime``（纳秒）解析口径；
   - 统一内存按 75% 折算成 ``vram_gb``。
3. **P2 多网关容灾**（``gateway_failover.py``）
   - 只有 502/503/504 才切换；4xx **不**切换；
   - 连续失败达阈值才切换；
   - 主网关恢复后自动切回。

⚠️ 本文件**绝不污染真机 ``agent.config.json``**：所有配置读写都走
``AgentConfig(config_path=<临时文件>)`` 独立实例（阶段八 P0 契约），
并在结束时校验真机配置**字节与 mtime 分毫不动**。
"""

from __future__ import annotations

import hashlib
import os
import sys
import tempfile
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[0]
sys.path.insert(0, str(REPO_ROOT))

import agent_config  # noqa: E402
import gateway_failover  # noqa: E402
import mesh_cluster  # noqa: E402
import telemetry  # noqa: E402
import yield_monitor  # noqa: E402

_PASS = 0
_FAIL = 0


def _record(name: str, ok: bool, detail: str = "") -> None:
    global _PASS, _FAIL
    if ok:
        _PASS += 1
        print("[PASS] %s | %s" % (name, detail))
    else:
        _FAIL += 1
        print("[FAIL] %s | %s" % (name, detail))


def _snapshot(path: Path):
    if not path.exists():
        return (None, None, None)
    raw = path.read_bytes()
    return (hashlib.sha256(raw).hexdigest(), path.stat().st_mtime_ns, len(raw))


# ---------------------------------------------------------------------------
# P0 · Pipeline Mesh
# ---------------------------------------------------------------------------

def _test_mesh() -> None:
    # ① 2×6GB ⇒ 12GB 集群 + 80 层对半切
    result = mesh_cluster.probe_mesh_cluster(
        peers=["192.168.1.102:52415"],
        self_vram_gb=6.0,
        simulated=[{"endpoint": "192.168.1.102:52415", "rtt_ms": 8.0, "vram_gb": 6.0}],
    )
    _record(
        "P0 2×6GB 合体 ⇒ 集群显存 12GB",
        abs(float(result["cluster_vram_gb"]) - 12.0) < 1e-6,
        "cluster_vram_gb=%s" % result["cluster_vram_gb"],
    )
    _record(
        "P0 合体后分片数 = 2（self + 1 peer）",
        int(result["mesh_shards"]) == 2 and int(result["mesh_peers_count"]) == 1,
        "mesh_shards=%s peers=%s" % (result["mesh_shards"], result["mesh_peers_count"]),
    )
    splits = result["layer_splits"]
    layers_ok = (
        len(splits) == 2
        and splits[0]["layer_start"] == 0 and splits[0]["layer_end"] == 39
        and splits[1]["layer_start"] == 40 and splits[1]["layer_end"] == 79
    )
    _record(
        "P0 80 层切分为 L0-39 / L40-79",
        layers_ok,
        " / ".join("%s(%s层)" % (s["layers"], s["layer_count"]) for s in splits),
    )
    total_layers = sum(int(s["layer_count"]) for s in splits)
    _record(
        "P0 分片层数之和恰好 = 80（不重不漏）",
        total_layers == 80,
        "sum=%d" % total_layers,
    )

    # ② RTT 超限 ⇒ 剔除
    slow = mesh_cluster.probe_mesh_cluster(
        peers=["192.168.1.102:52415"],
        self_vram_gb=6.0,
        simulated=[{"endpoint": "192.168.1.102:52415", "rtt_ms": 30.0, "vram_gb": 6.0}],
    )
    _record(
        "P0 RTT 30ms ≥ 25ms 上限 ⇒ 该 peer 被剔除",
        int(slow["mesh_peers_count"]) == 0
        and len(slow["rejected_peers"]) == 1
        and slow["rejected_peers"][0].get("reject_reason") == "rtt_too_high",
        "rejected=%s" % [p.get("reject_reason") for p in slow["rejected_peers"]],
    )
    _record(
        "P0 全部 peer 被剔除 ⇒ 不启用合体（fail closed）",
        bool(slow["mesh_enabled"]) is False,
        "mesh_enabled=%r" % slow["mesh_enabled"],
    )

    # ③ 显存未知 ⇒ 剔除
    unknown = mesh_cluster.probe_mesh_cluster(
        peers=["192.168.1.103:52415"],
        self_vram_gb=6.0,
        simulated=[{"endpoint": "192.168.1.103:52415", "rtt_ms": 5.0, "vram_gb": 0}],
    )
    _record(
        "P0 peer 未上报显存 ⇒ 被剔除（不把未知当有）",
        int(unknown["mesh_peers_count"]) == 0
        and unknown["rejected_peers"][0].get("reject_reason") == "vram_unknown",
        "rejected=%s" % [p.get("reject_reason") for p in unknown["rejected_peers"]],
    )

    # ④ 不可达 ⇒ 剔除（真实 socket：127.0.0.1:1 必然连不上）
    unreachable = mesh_cluster.probe_mesh_cluster(
        peers=["127.0.0.1:1"], self_vram_gb=6.0, timeout_sec=0.3)
    _record(
        "P0 不可达 peer ⇒ 被剔除",
        int(unreachable["mesh_peers_count"]) == 0
        and unreachable["rejected_peers"][0].get("reject_reason") == "unreachable",
        "rejected=%s" % [p.get("reject_reason") for p in unreachable["rejected_peers"]],
    )

    # ⑤ 环境变量注入（验收可复现）
    os.environ["APIPAW_MESH_SIMULATED_PEERS"] = (
        '[{"endpoint":"192.168.1.102:52415","rtt_ms":8.0,"vram_gb":6.0}]')
    try:
        injected = mesh_cluster.probe_mesh_cluster(
            peers=["192.168.1.102:52415"], self_vram_gb=6.0)
        _record(
            "P0 环境变量注入的模拟拓扑可复现（12GB / 2 分片）",
            abs(float(injected["cluster_vram_gb"]) - 12.0) < 1e-6
            and int(injected["mesh_shards"]) == 2,
            "cluster_vram_gb=%s shards=%s"
            % (injected["cluster_vram_gb"], injected["mesh_shards"]),
        )
    finally:
        os.environ.pop("APIPAW_MESH_SIMULATED_PEERS", None)

    # ⑥ peer 地址解析
    _record(
        "P0 peer 地址解析：host:port / [::1]:port / 纯 host",
        mesh_cluster.parse_peer_endpoint("192.168.1.102:52415") == ("192.168.1.102", 52415)
        and mesh_cluster.parse_peer_endpoint("[::1]:52415") == ("::1", 52415)
        and mesh_cluster.parse_peer_endpoint("192.168.1.102") == ("192.168.1.102", 0),
        "ok",
    )


# ---------------------------------------------------------------------------
# P1 · macOS 空闲检测 + Apple Silicon 统一内存
# ---------------------------------------------------------------------------

def _test_macos() -> None:
    sample = (
        '+-o Root  <class IOHIDSystem, id 0x100000d8b, registered, matched, '
        'active, busy 0 (0 ms), retain 6>\n'
        '    "HIDIdleTime" = 1234567890\n'
        '    "HIDSystemUniqueID" = "0x1234"\n'
    )
    parsed = yield_monitor._parse_macos_idle_seconds(sample)
    _record(
        "P1 ioreg HIDIdleTime 纳秒 ⇒ 秒（1.23456890s）",
        parsed is not None and abs(float(parsed) - 1.235) < 1e-6,
        "parsed=%r" % (parsed,),
    )
    _record(
        "P1 输出里没有 HIDIdleTime ⇒ None（保持现状，不误判为忙）",
        yield_monitor._parse_macos_idle_seconds("nothing here") is None,
        "ok",
    )
    _record(
        "P1 空输出 ⇒ None",
        yield_monitor._parse_macos_idle_seconds("") is None,
        "ok",
    )

    # Apple Silicon 统一内存折算（注入模式，跨平台可测）
    os.environ["APIPAW_MOCK_APPLE_MEMORY_GB"] = "16"
    os.environ["APIPAW_MOCK_APPLE_CHIP"] = "Apple M2 Pro"
    try:
        apple = telemetry.detect_apple_silicon()
    finally:
        os.environ.pop("APIPAW_MOCK_APPLE_MEMORY_GB", None)
        os.environ.pop("APIPAW_MOCK_APPLE_CHIP", None)
    _record(
        "P1 Apple Silicon 识别（注入）",
        bool(apple.get("is_apple_silicon")) and apple.get("chip") == "Apple M2 Pro",
        "chip=%s" % apple.get("chip"),
    )
    _record(
        "P1 统一内存 16GB ⇒ vram 12GB（75% 折算）",
        abs(float(apple.get("vram_gb") or 0) - 12.0) < 1e-6,
        "unified=%s vram=%s" % (apple.get("unified_memory_gb"), apple.get("vram_gb")),
    )
    _record(
        "P1 折算比例常量 = 0.75",
        abs(telemetry.APPLE_UNIFIED_MEMORY_VRAM_RATIO - 0.75) < 1e-9,
        "ratio=%s" % telemetry.APPLE_UNIFIED_MEMORY_VRAM_RATIO,
    )


# ---------------------------------------------------------------------------
# P2 · 多网关主备容灾
# ---------------------------------------------------------------------------

def _test_failover() -> None:
    fo = gateway_failover.GatewayFailover(
        "https://primary.apipaw.com",
        ["https://backup.apipaw.com", "https://backup2.apipaw.com"],
        threshold=3,
    )
    _record(
        "P2 网关列表去重保序（主在前）",
        fo.gateways == [
            "https://primary.apipaw.com",
            "https://backup.apipaw.com",
            "https://backup2.apipaw.com",
        ],
        "gateways=%s" % fo.gateways,
    )

    # ① 4xx 不切换
    for _ in range(5):
        fo.record_failure(status_code=400)
    _record(
        "P2 4xx（请求不对）⇒ 绝不切网关",
        fo.current_index == 0 and fo.consecutive_failures == 0,
        "index=%s failures=%s" % (fo.current_index, fo.consecutive_failures),
    )

    # ② 未达阈值不切换
    fo.record_failure(status_code=502)
    fo.record_failure(status_code=503)
    _record(
        "P2 连续失败未达阈值（2/3）⇒ 不切换",
        fo.current_index == 0 and fo.consecutive_failures == 2,
        "index=%s failures=%s" % (fo.current_index, fo.consecutive_failures),
    )

    # ③ 达阈值 ⇒ 切到备用
    switched = fo.record_failure(status_code=504)
    _record(
        "P2 连续失败达阈值（3/3）⇒ 自动切到备用网关",
        switched and fo.current_index == 1 and fo.is_failover,
        "index=%s switched=%r" % (fo.current_index, switched),
    )
    _record(
        "P2 切换后连续失败计数归零",
        fo.consecutive_failures == 0,
        "failures=%s" % fo.consecutive_failures,
    )

    # ④ 主网关恢复 ⇒ 自动切回
    failed_back = fo.record_success(primary_probe=lambda: True)
    _record(
        "P2 主网关探测通过 ⇒ 自动切回主网关",
        failed_back and fo.current_index == 0 and not fo.is_failover,
        "index=%s" % fo.current_index,
    )

    # ⑤ 主网关仍不健康 ⇒ 不切回
    fo.record_failure(status_code=502)
    fo.record_failure(status_code=502)
    fo.record_failure(status_code=502)
    _record(
        "P2 切到备用后处于备用态",
        fo.current_index == 1 and fo.is_failover,
        "index=%s" % fo.current_index,
    )
    no_failback = fo.record_success(primary_probe=lambda: False)
    _record(
        "P2 主网关仍不健康 ⇒ 不切回（留在备用）",
        (not no_failback) and fo.current_index == 1,
        "index=%s" % fo.current_index,
    )

    # ⑥ 状态快照
    status = fo.status()
    _record(
        "P2 状态快照含主备信息（心跳/面板上报用）",
        status.get("gateway_count") == 3
        and status.get("gateway_primary") == "https://primary.apipaw.com"
        and status.get("gateway_is_backup") is True,
        "status=%s" % {k: status.get(k) for k in
                        ("gateway_url", "gateway_count", "gateway_is_backup")},
    )

    # ⑦ 地址换算
    _record(
        "P2 网关基址 ⇒ WS / 心跳端点",
        gateway_failover.to_ws_url("https://backup.apipaw.com")
        == "wss://backup.apipaw.com/api/v1/node/stream"
        and gateway_failover.to_heartbeat_url("https://backup.apipaw.com")
        == "https://backup.apipaw.com/api/v1/nodes/heartbeat",
        "ok",
    )
    _record(
        "P2 已是完整端点的地址不被重复拼接",
        gateway_failover.to_ws_url("wss://x.apipaw.com/api/v1/node/stream")
        == "wss://x.apipaw.com/api/v1/node/stream",
        "ok",
    )


# ---------------------------------------------------------------------------
# 配置隔离（真机 agent.config.json 分毫不动）
# ---------------------------------------------------------------------------

def _test_config_isolation() -> None:
    real_path = agent_config.PROJECT_ROOT / agent_config.CONFIG_FILE_NAME
    before = _snapshot(real_path)

    tmp_dir = tempfile.mkdtemp(prefix="apipaw_p20_")
    tmp_path = Path(tmp_dir) / "agent.config.json"
    isolated = agent_config.AgentConfig(config_path=tmp_path)
    isolated.update(
        mesh_peers=["192.168.1.102:52415"],
        mesh_enabled=True,
        mesh_mode="Pipeline Shards",
        mesh_max_rtt_ms=25.0,
        backup_gateway_urls=["https://backup.apipaw.com"],
        gateway_failover_threshold=3,
    )
    saved = tmp_path.read_text(encoding="utf-8")
    _record(
        "配置：Mesh / 多网关字段可写入独立实例",
        '"mesh_peers"' in saved and '"backup_gateway_urls"' in saved
        and '"gateway_failover_threshold"' in saved,
        "临时配置已写入",
    )
    _record(
        "配置：读回属性正确",
        list(isolated.mesh_peers) == ["192.168.1.102:52415"]
        and bool(isolated.mesh_enabled) is True
        and list(isolated.backup_gateway_urls) == ["https://backup.apipaw.com"]
        and int(isolated.gateway_failover_threshold) == 3,
        "mesh_peers=%s backups=%s" % (isolated.mesh_peers, isolated.backup_gateway_urls),
    )

    after = _snapshot(real_path)
    _record(
        "配置隔离：真机 agent.config.json 内容分毫不动",
        before[0] == after[0] and before[2] == after[2],
        "sha256 %s → %s" % (str(before[0])[:12], str(after[0])[:12]),
    )
    _record(
        "配置隔离：真机 agent.config.json mtime 分毫不动",
        before[1] == after[1],
        "mtime_ns %s → %s" % (before[1], after[1]),
    )


def main() -> int:
    print("===== 阶段二十 P0/P1/P2 · Agent 侧自检（沙箱）=====")
    _test_mesh()
    _test_macos()
    _test_failover()
    _test_config_isolation()
    print("")
    print("===== 阶段二十 Agent 自检：%d/%d 通过 =====" % (_PASS, _PASS + _FAIL))
    return 0 if _FAIL == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
