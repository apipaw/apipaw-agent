"""
gateway_failover.py — 阶段二十 P2 · 多网关主备自动容灾

目标
----
消除「单网关单点故障」：主网关不可用（连接失败）或返回
``502 / 503 / 504``（网关层错误）时，节点**自动**故障转移到备用网关；
主网关恢复后**自动切回**。

设计要点
--------
1. **不因一次抖动搬家**：连续失败次数达到阈值（默认 3）才切换 ——
   一次网络抖动就把节点搬到备机，反而制造新的问题。
2. **网关层错误才算故障**：``502/503/504`` 才是「网关自己不行」；
   ``4xx`` 是「请求不对」（改请求也白搭），``200`` 是成功。
3. **恢复即切回**：备用网关工作正常后，若主网关探测通过 ⇒ 切回主网关，
   避免节点长期滞留在备机上（备机的容量/信誉会被长期消耗）。
4. **纯逻辑、可单测**：本模块**不碰网络**（探测由调用方注入），
   便于在 CI 里确定性地验证切换/切回逻辑。

对外接口
--------
* :class:`GatewayFailover` —— 主备状态机
* :func:`normalize_gateway_list` —— 规整网关列表（去重、保序）
"""

from __future__ import annotations

import threading
from typing import Any, Callable, Dict, List, Optional, Sequence

# 触发故障转移的 HTTP 状态码（网关层错误）
FAILOVER_STATUS_CODES = (502, 503, 504)


def normalize_gateway_list(primary: str, backups: Optional[Sequence[str]] = None) -> List[str]:
    """规整网关列表：**主网关在前**，备用网关去重保序。"""
    result: List[str] = []
    for url in [primary] + list(backups or []):
        text = str(url or "").strip()
        if not text:
            continue
        if text in result:
            continue
        result.append(text)
    return result


def to_ws_url(base: str) -> str:
    """把**网关基址**转成 WS 端点地址。

    ``https://backup.apipaw.com`` ⇒ ``wss://backup.apipaw.com/api/v1/node/stream``
    已经是完整端点地址的**原样返回**（不重复拼路径）。
    """
    text = str(base or "").strip()
    if not text:
        return ""
    if "/api/v1/node/stream" in text:
        return text
    text = text.rstrip("/")
    if text.startswith("https://"):
        text = "wss://" + text[len("https://"):]
    elif text.startswith("http://"):
        text = "ws://" + text[len("http://"):]
    return text + "/api/v1/node/stream"


def to_heartbeat_url(base: str) -> str:
    """把**网关基址**转成心跳端点地址。"""
    text = str(base or "").strip()
    if not text:
        return ""
    if "/api/v1/nodes/heartbeat" in text:
        return text
    return text.rstrip("/") + "/api/v1/nodes/heartbeat"


class GatewayFailover:
    """主备网关状态机。"""

    def __init__(self,
                 primary_url: str,
                 backup_urls: Optional[Sequence[str]] = None,
                 threshold: int = 3) -> None:
        self._lock = threading.RLock()
        self._gateways = normalize_gateway_list(primary_url, backup_urls)
        self._primary = self._gateways[0] if self._gateways else ""
        self._index = 0
        self._consecutive_failures = 0
        self._threshold = max(1, int(threshold or 1))
        self._switches = 0
        self._last_switch_at = 0.0

    # ───────────────────────── 只读属性 ─────────────────────────

    @property
    def gateways(self) -> List[str]:
        with self._lock:
            return list(self._gateways)

    @property
    def current_url(self) -> str:
        with self._lock:
            if not self._gateways:
                return ""
            return self._gateways[self._index]

    @property
    def current_index(self) -> int:
        with self._lock:
            return self._index

    @property
    def consecutive_failures(self) -> int:
        with self._lock:
            return self._consecutive_failures

    @property
    def is_failover(self) -> bool:
        """当前是否正跑在**备用**网关上。"""
        with self._lock:
            return self._index > 0

    # ───────────────────────── 状态迁移 ─────────────────────────

    def record_success(self, primary_probe: Optional[Callable[[], bool]] = None) -> bool:
        """记录一次成功。

        若当前在备用网关上，且 ``primary_probe()`` 判定主网关已恢复
        ⇒ **切回主网关**（返回 ``True`` 表示发生了切回）。

        ⚠️ ``primary_probe`` 必须是**廉价**的（一次 TCP/HTTP 探测），
        不能拖慢心跳/WS 主循环。
        """
        with self._lock:
            self._consecutive_failures = 0
            if self._index == 0:
                return False
            if primary_probe is None:
                return False
        try:
            healthy = bool(primary_probe())
        except Exception:  # noqa: BLE001 - 探测异常一律不切回
            return False
        if not healthy:
            return False
        with self._lock:
            if self._index == 0:
                return False
            self._index = 0
            self._switches += 1
            self._last_switch_at = 0.0
            return True

    def record_failure(self, status_code: Optional[int] = None) -> bool:
        """记录一次失败；达到阈值则切到下一个网关。

        返回 ``True`` 表示**发生了切换**。

        ⚠️ ``status_code`` 非空且**不在** ``502/503/504`` 里 ⇒ 视为
        「请求本身的问题」而非「网关故障」⇒ **不切换**（否则一个 400
        就能把节点踢到备机上去）。
        """
        with self._lock:
            if status_code is not None and int(status_code) not in FAILOVER_STATUS_CODES:
                # 4xx 之类：不是网关故障，但也不清零失败计数（请求仍没成功）
                return False
            self._consecutive_failures += 1
            if len(self._gateways) <= 1:
                return False
            if self._consecutive_failures < self._threshold:
                return False
            self._index = (self._index + 1) % len(self._gateways)
            self._consecutive_failures = 0
            self._switches += 1
            return True

    # ───────────────────────── 状态快照 ─────────────────────────

    def status(self) -> Dict[str, Any]:
        """当前容灾状态（心跳/面板上报用）。"""
        with self._lock:
            return {
                "gateway_url": self.current_url,
                "gateway_index": self._index,
                "gateway_count": len(self._gateways),
                "gateway_primary": self._primary,
                "gateway_is_backup": self._index > 0,
                "gateway_consecutive_failures": self._consecutive_failures,
                "gateway_failover_threshold": self._threshold,
                "gateway_switches": self._switches,
                "gateways": list(self._gateways),
            }


__all__ = [
    "FAILOVER_STATUS_CODES",
    "GatewayFailover",
    "normalize_gateway_list",
    "to_ws_url",
    "to_heartbeat_url",
]
