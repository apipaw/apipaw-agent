"""
mesh_cluster.py — 阶段二十 P0 · 局域网多机张量切片合体（Pipeline Mesh）

目标
----
把**同一局域网内**的多台小显存 PC 组成一个「张量切片」集群：
80 层的大模型按各节点显存**比例切层**，每台机器只加载自己那几层，
推理时逐层接力 ⇒ **多台小显存 PC 合体出大显存节点的算力**。

概念
----
* **Peer（协同节点）**：局域网内另一台跑着 APIPaw Agent 的机器，
  形如 ``192.168.1.102:52415``。
* **RTT（往返时延）**：跨机传张量片段的物理下限。局域网内必须
  **< 25ms**（``MESH_MAX_RTT_MS``）才允许合体 —— 超过这个数，
  逐层接力的通信开销会吃掉推理收益，此时**拒绝合体**（fail closed）。
* **分片（shard）**：一台机器承担模型的一层区间，如 ``L0-39``。

安全边界（为什么这么设计）
--------------------------
1. **合体只增不减容错**：探测不到显存的 peer **不参与显存聚合**
   （宁可少合一台，也不把「未知」当「有」）。
2. **RTT 超标的 peer 直接剔除**：其余 peer 仍可组成集群。
3. **本机显存永远计入**：本机是集群的锚点。
4. **可注入模拟拓扑**：验收/CI 环境没有真实局域网 ⇒ 支持用环境变量
   ``APIPAW_MESH_SIMULATED_PEERS`` 注入一份 Peer 拓扑（JSON），
   使端到端验收可复现。注入只影响**探测结果**，不改变任何计算口径。

对外接口
--------
* :func:`probe_mesh_cluster` —— 探测 + 聚合 + 算分片（主入口）
* :func:`compute_layer_splits` —— 纯函数：按显存比例切层
* :func:`parse_peer_endpoint` —— 解析 ``host:port``
* :func:`mesh_status_from_config` —— 从配置构造探测参数（面板/心跳共用）
"""

from __future__ import annotations

import json
import os
import socket
import threading
import time
from typing import Any, Dict, Iterable, List, Optional, Tuple

# 探测结果缓存（心跳 30s 一轮，不该每轮都去连一遍 peer）
_MESH_STATUS_CACHE: Dict[str, Any] = {"at": 0.0, "value": None}
_MESH_STATUS_LOCK = threading.Lock()

# 局域网合体的 RTT 上限（毫秒）—— 超过就不合体
MESH_MAX_RTT_MS = 25.0

# 默认模型总层数（切层基准）
MESH_TOTAL_LAYERS = 80

# 协议版本（云端据此判断节点是否支持 Mesh）
MESH_PROTOCOL_VERSION = "mesh-v1"

# 合体模式
MESH_MODE_PIPELINE = "Pipeline Shards"
MESH_MODE_LLAMA_RPC = "llama.cpp RPC"

# v1.0.19：探测对端 /health 时使用的 UA。
# ⚠️ 不要用 urllib 默认的 Python-urllib/x.y —— 若对端挂在 Cloudflare
# 之后会被边缘直接拦成 403（真机 Bug C 的同源问题）。
_PEER_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 APIPawAgent/1.0.20"
)


# --------------------------------------------------------------------------
# 基础工具
# --------------------------------------------------------------------------

def parse_peer_endpoint(spec: Any) -> Tuple[str, int]:
    """解析 peer 描述 ``host:port``；非法输入回落到 ``("", 0)``。

    也接受 dict（``{"host": ..., "port": ...}``）与纯 host（补默认端口）。
    """
    if isinstance(spec, dict):
        host = str(spec.get("host") or "").strip()
        try:
            port = int(spec.get("port") or 0)
        except (TypeError, ValueError):
            port = 0
        return host, port
    text = str(spec or "").strip()
    if not text:
        return "", 0
    if text.startswith("["):  # [::1]:52415
        try:
            host, _, port_text = text.rpartition("]:")
            return host.lstrip("["), int(port_text)
        except ValueError:
            return text, 0
    if ":" in text:
        host, _, port_text = text.rpartition(":")
        try:
            return host.strip(), int(port_text)
        except ValueError:
            return host.strip(), 0
    return text, 0


def _to_float(value: Any, default: Optional[float] = None) -> Optional[float]:
    try:
        if value is None or value == "":
            return default
        return float(value)
    except (TypeError, ValueError):
        return default


def _simulated_peers() -> Optional[List[Dict[str, Any]]]:
    """读取环境变量注入的**模拟 Peer 拓扑**。

    格式::

        APIPAW_MESH_SIMULATED_PEERS='[{"endpoint":"192.168.1.102:52415",
                                       "rtt_ms":8.0,"vram_gb":6.0}]'

    ⚠️ 注入**只影响探测结果**，分片/显存聚合口径与真实探测完全一致 ——
    否则验收验的是另一套算法，绿了也不能说明问题。
    """
    raw = os.getenv("APIPAW_MESH_SIMULATED_PEERS", "").strip()
    if not raw:
        return None
    try:
        loaded = json.loads(raw)
    except (ValueError, TypeError):
        print("[mesh] APIPAW_MESH_SIMULATED_PEERS 不是合法 JSON，忽略注入")
        return None
    if not isinstance(loaded, list):
        return None
    peers: List[Dict[str, Any]] = []
    for item in loaded:
        if not isinstance(item, dict):
            continue
        endpoint = str(item.get("endpoint") or "").strip()
        if not endpoint:
            continue
        peers.append({
            "endpoint": endpoint,
            "rtt_ms": _to_float(item.get("rtt_ms"), 0.0) or 0.0,
            "vram_gb": _to_float(item.get("vram_gb"), 0.0) or 0.0,
        })
    return peers


# --------------------------------------------------------------------------
# 单个 Peer 探测
# --------------------------------------------------------------------------

def probe_peer(endpoint: str, timeout_sec: float = 1.0) -> Dict[str, Any]:
    """探测一个协同节点：连通性 + RTT + 可用显存。

    流程：
      1. TCP 连接（``socket.create_connection``）⇒ 测出**网络 RTT**；
      2. HTTP ``GET /health`` ⇒ 取对方的 ``gpu.vram_total_gb``（真实显存）。

    ⚠️ 任何一步失败都**不抛异常**，只把结果标成不可达 —— 探测是后台行为，
    绝不能因为一台 peer 掉线就把节点本体拖垮。
    """
    host, port = parse_peer_endpoint(endpoint)
    result: Dict[str, Any] = {
        "endpoint": endpoint,
        "host": host,
        "port": port,
        "reachable": False,
        "rtt_ms": None,
        "vram_gb": None,
        "error": "",
    }
    if not host or port <= 0:
        result["error"] = "peer 地址非法"
        return result

    started = time.time()
    try:
        with socket.create_connection((host, port), timeout=timeout_sec):
            pass
    except Exception as exc:  # noqa: BLE001 - 探测失败只记录，不抛
        result["error"] = "连接失败: %s" % exc
        return result
    result["reachable"] = True
    result["rtt_ms"] = round((time.time() - started) * 1000.0, 3)

    # 取对方显存（HTTP /health）
    try:
        import urllib.request  # 局部导入：非必要依赖不进 import 期

        url = "http://%s:%d/health" % (host, port)
        # v1.0.19：带合规 UA —— 对端若也挂在 Cloudflare 后面，
        # 默认的 Python-urllib UA 会被边缘拦成 403。
        request = urllib.request.Request(
            url, headers={"User-Agent": _PEER_USER_AGENT,
                          "Accept": "application/json"})
        with urllib.request.urlopen(request, timeout=timeout_sec) as resp:
            payload = json.loads(resp.read().decode("utf-8", "replace"))
        gpu = payload.get("gpu") or {}
        vram = _to_float(gpu.get("vram_total_gb"), None)
        if vram is None:
            vram = _to_float((payload.get("gpu") or {}).get("vram_total"), None)
        if vram is not None and vram > 0:
            result["vram_gb"] = round(vram, 3)
    except Exception as exc:  # noqa: BLE001 - 取不到显存也不影响连通性判定
        result["error"] = "读取对端显存失败: %s" % exc
    return result


# --------------------------------------------------------------------------
# 分片计算
# --------------------------------------------------------------------------

def compute_layer_splits(vram_list: Iterable[float],
                          total_layers: int = MESH_TOTAL_LAYERS,
                          labels: Optional[List[str]] = None) -> List[Dict[str, Any]]:
    """按各节点**显存比例**把模型层切给它们。

    例：两台 6GB 节点 + 80 层 ⇒ ``L0-39`` 与 ``L40-79``（各 40 层）。

    ⚠️ 余数分配规则：按小数部分从大到小补给前面的节点 —— 保证
    「所有分片层数之和 == total_layers」，不重不漏。
    """
    values = [max(0.0, float(v or 0.0)) for v in vram_list]
    names = list(labels or [])
    while len(names) < len(values):
        names.append("node-%d" % (len(names) + 1))
    layers = max(1, int(total_layers or MESH_TOTAL_LAYERS))

    total_vram = sum(values)
    if total_vram <= 0:
        return []

    raw = [layers * (v / total_vram) for v in values]
    base = [int(x) for x in raw]
    remainder = layers - sum(base)
    # 小数部分大者优先补 1 层
    order = sorted(range(len(values)), key=lambda i: (-(raw[i] - base[i]), i))
    for idx in order[:max(0, remainder)]:
        base[idx] += 1

    splits: List[Dict[str, Any]] = []
    cursor = 0
    for idx, count in enumerate(base):
        if count <= 0:
            continue
        start = cursor
        end = cursor + count - 1
        cursor = end + 1
        splits.append({
            "index": len(splits),
            "node": names[idx],
            "label": names[idx],
            "vram_gb": round(values[idx], 3),
            "layers": "L%d-%d" % (start, end),
            "layer_start": start,
            "layer_end": end,
            "start_layer": start,
            "end_layer": end,
            "layer_count": count,
            "share": round(values[idx] / total_vram, 6) if total_vram else 0.0,
        })
    return splits


# --------------------------------------------------------------------------
# 主入口：探测 + 聚合
# --------------------------------------------------------------------------

def probe_mesh_cluster(peers: Optional[List[Any]] = None,
                       self_vram_gb: float = 0.0,
                       max_rtt_ms: float = MESH_MAX_RTT_MS,
                       total_layers: int = MESH_TOTAL_LAYERS,
                       timeout_sec: float = 1.0,
                       mode: str = MESH_MODE_PIPELINE,
                       simulated: Optional[List[Dict[str, Any]]] = None) -> Dict[str, Any]:
    """探测局域网协同节点并计算合体后的显存与分片。

    参数
    ----
    peers
        协同节点列表，形如 ``["192.168.1.102:52415"]``。
    self_vram_gb
        本机显存（GB）。**永远计入**集群总量。
    max_rtt_ms
        允许合体的 RTT 上限（默认 25ms）。
    simulated
        注入的模拟 Peer 拓扑（``[{"endpoint","rtt_ms","vram_gb"}]``）。
        传 ``None`` 时自动读环境变量 ``APIPAW_MESH_SIMULATED_PEERS``。

    返回
    ----
    dict
        ``mesh_enabled`` / ``cluster_vram_gb`` / ``mesh_shards`` /
        ``mesh_rtt_ms`` / ``mesh_peers_count`` / ``layer_splits`` /
        ``peers`` / ``rejected_peers``
    """
    peer_specs = list(peers or [])
    injected = simulated if simulated is not None else _simulated_peers()

    probed: List[Dict[str, Any]] = []
    if injected is not None:
        for item in injected:
            endpoint = str(item.get("endpoint") or "").strip()
            if not endpoint:
                continue
            rtt = _to_float(item.get("rtt_ms"), 0.0) or 0.0
            vram = _to_float(item.get("vram_gb"), 0.0) or 0.0
            probed.append({
                "endpoint": endpoint,
                "host": parse_peer_endpoint(endpoint)[0],
                "port": parse_peer_endpoint(endpoint)[1],
                "reachable": True,
                "rtt_ms": round(rtt, 3),
                "vram_gb": round(vram, 3) if vram > 0 else None,
                "error": "" if vram > 0 else "未上报显存",
                "simulated": True,
            })
    else:
        for spec in peer_specs:
            endpoint = str(spec.get("endpoint") if isinstance(spec, dict) else spec).strip()
            if not endpoint:
                continue
            probed.append(probe_peer(endpoint, timeout_sec=timeout_sec))

    accepted: List[Dict[str, Any]] = []
    rejected: List[Dict[str, Any]] = []
    limit = float(max_rtt_ms or MESH_MAX_RTT_MS)
    for peer in probed:
        if not peer.get("reachable"):
            peer["reject_reason"] = "unreachable"
            rejected.append(peer)
            continue
        rtt = _to_float(peer.get("rtt_ms"), None)
        if rtt is None or rtt >= limit:
            peer["reject_reason"] = "rtt_too_high"
            rejected.append(peer)
            continue
        if peer.get("vram_gb") is None or float(peer.get("vram_gb") or 0) <= 0:
            peer["reject_reason"] = "vram_unknown"
            rejected.append(peer)
            continue
        accepted.append(peer)

    self_vram = float(self_vram_gb or 0.0)
    cluster_vram = self_vram + sum(float(p.get("vram_gb") or 0.0) for p in accepted)

    vram_series = [self_vram] + [float(p.get("vram_gb") or 0.0) for p in accepted]
    labels = ["self"] + [str(p.get("endpoint") or "") for p in accepted]
    splits = compute_layer_splits(vram_series, total_layers=total_layers, labels=labels)

    rtt_values = [float(p.get("rtt_ms") or 0.0) for p in accepted]
    mesh_rtt = round(max(rtt_values), 3) if rtt_values else 0.0

    return {
        "mesh_enabled": bool(accepted),
        "mesh_protocol_version": MESH_PROTOCOL_VERSION,
        "mesh_mode": str(mode or MESH_MODE_PIPELINE),
        "cluster_vram_gb": round(cluster_vram, 3),
        "mesh_shards": len(splits),
        "mesh_rtt_ms": mesh_rtt,
        "mesh_peers_count": len(accepted),
        "mesh_peers_configured": len(peer_specs),
        "mesh_max_rtt_ms": limit,
        "total_layers": int(total_layers or MESH_TOTAL_LAYERS),
        "self_vram_gb": round(self_vram, 3),
        "layer_splits": splits,
        "peers": probed,
        "rejected_peers": rejected,
    }


def mesh_status_from_config(cfg: Any, max_age_sec: float = 30.0) -> Dict[str, Any]:
    """从 ``agent.config.json`` 读出 Mesh 参数并执行一次探测。

    面板（``/ui``）与心跳上报共用这个入口 ⇒ 面板上看到的分片拓扑
    与云端拿到的**完全是同一份**（不会出现「面板好看、云端没有」）。

    ``max_age_sec`` 是**探测结果缓存窗口**（默认 30s）：探测要真连 peer，
    每个心跳周期都探一遍会把心跳拖慢；缓存保证上送的分片拓扑足够新鲜，
    又不会把心跳周期变成网络探测。面板传 ``max_age_sec=0`` 可强制刷新。
    """
    def _get(name: str, default: Any) -> Any:
        try:
            value = getattr(cfg, name, default)
        except Exception:  # noqa: BLE001 - 配置异常一律回落默认值
            return default
        return default if value is None else value

    global _MESH_STATUS_CACHE
    with _MESH_STATUS_LOCK:
        cached = _MESH_STATUS_CACHE.get("value")
        at = float(_MESH_STATUS_CACHE.get("at") or 0.0)
        if (cached is not None and max_age_sec > 0
                and (time.time() - at) < float(max_age_sec)):
            return dict(cached)

    peers = _get("mesh_peers", []) or []
    if isinstance(peers, str):
        peers = [p.strip() for p in peers.split(",") if p.strip()]
    enabled = bool(_get("mesh_enabled", False))
    self_vram = _to_float(_get("max_vram_gb", 0.0), 0.0) or 0.0
    # 本机真实显存优先（nvidia-smi / Apple Silicon 统一内存折算）
    try:
        from telemetry import collect_gpu_metrics_cached

        gpu = collect_gpu_metrics_cached()
        real = _to_float(gpu.get("vram_total_gb"), None)
        if real and real > 0:
            self_vram = real
    except Exception:  # noqa: BLE001 - 采集失败就用配置值
        pass

    result = probe_mesh_cluster(
        peers=peers,
        self_vram_gb=self_vram,
        max_rtt_ms=_to_float(_get("mesh_max_rtt_ms", MESH_MAX_RTT_MS), MESH_MAX_RTT_MS) or MESH_MAX_RTT_MS,
        total_layers=int(_to_float(_get("mesh_total_layers", MESH_TOTAL_LAYERS), MESH_TOTAL_LAYERS) or MESH_TOTAL_LAYERS),
        mode=str(_get("mesh_mode", MESH_MODE_PIPELINE) or MESH_MODE_PIPELINE),
    )
    # 配置里没开 ⇒ 仍然探测（面板要看），但**不启用**合体
    result["mesh_configured"] = enabled
    result["mesh_enabled"] = bool(enabled and result.get("mesh_peers_count"))
    with _MESH_STATUS_LOCK:
        _MESH_STATUS_CACHE["at"] = time.time()
        _MESH_STATUS_CACHE["value"] = dict(result)
    return result


__all__ = [
    "MESH_MAX_RTT_MS",
    "MESH_TOTAL_LAYERS",
    "MESH_PROTOCOL_VERSION",
    "MESH_MODE_PIPELINE",
    "MESH_MODE_LLAMA_RPC",
    "parse_peer_endpoint",
    "probe_peer",
    "probe_mesh_cluster",
    "compute_layer_splits",
    "mesh_status_from_config",
]
