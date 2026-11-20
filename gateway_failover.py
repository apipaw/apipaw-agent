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
* :func:`validate_backup_gateway_urls` —— 阶段二十二 P1 · 备用网关**可信性校验**
"""

from __future__ import annotations

import os
import threading
from typing import Any, Callable, Dict, List, Optional, Sequence
from urllib.parse import urlsplit

# 触发故障转移的 HTTP 状态码（网关层错误）
FAILOVER_STATUS_CODES = (502, 503, 504)

# --------------------------------------------------------------------------
# 阶段二十二 P1 · 备用网关白名单
# --------------------------------------------------------------------------
# 为什么必须收紧（审计 §5A.2 第 12 条）：
#   ``backup_gateway_urls`` 是**面板可写**的配置项（``/api/mesh/prefs``），
#   而节点会把 **Node Secret** 通过 WS 握手发给「网关地址」。
#   一旦这个地址可被改成 ``ws://evil.example.com``，节点就会**主动把
#   节点凭证送到攻击者手里** —— 攻击者拿它就能冒充本节点接单、骗取分账。
#
#   所以：
#   1. 协议只允许 **wss:// / https://**（明文 ws://http:// 一律拒绝）；
#   2. 域名必须在**信任域白名单**内（默认 ``*.apipaw.com``，
#      可用环境变量 ``APIPAW_TRUSTED_GATEWAY_DOMAINS`` 追加）。
DEFAULT_TRUSTED_GATEWAY_DOMAINS = ("apipaw.com",)
#: 允许的协议（其余一律拒绝）
TRUSTED_GATEWAY_SCHEMES = ("wss://", "https://")
#: 信任域扩展的环境变量名
TRUSTED_GATEWAY_DOMAINS_ENV = "APIPAW_TRUSTED_GATEWAY_DOMAINS"


def trusted_gateway_domains() -> List[str]:
    """当前生效的**信任域白名单**（小写、去点号、去重）。

    默认只有 ``apipaw.com``；用 ``APIPAW_TRUSTED_GATEWAY_DOMAINS``
    以英文逗号追加（例如 ``example.com,lab.internal``）。
    """
    domains: List[str] = []
    for raw in list(DEFAULT_TRUSTED_GATEWAY_DOMAINS) + [
        str(os.environ.get(TRUSTED_GATEWAY_DOMAINS_ENV) or "").split(",")
    ]:
        for item in (raw if isinstance(raw, (list, tuple)) else [raw]):
            text = str(item or "").strip().lower().lstrip(".")
            if text and text not in domains:
                domains.append(text)
    return domains


def _host_matches(host: str, domain: str) -> bool:
    """主机名是否命中信任域（含 ``*.domain`` 通配）。"""
    if not host or not domain:
        return False
    return host == domain or host.endswith("." + domain)


def is_trusted_gateway_url(url: str,
                           domains: Optional[Sequence[str]] = None) -> bool:
    """单个网关地址是否**可信**。

    判定口径：
      - 协议必须是 ``wss://`` 或 ``https://``；
      - 主机名必须命中信任域白名单（精确匹配或子域通配）。
    """
    text = str(url or "").strip()
    if not text:
        return False
    if not text.lower().startswith(TRUSTED_GATEWAY_SCHEMES):
        return False
    try:
        host = (urlsplit(text).hostname or "").lower()
    except ValueError:
        return False
    if not host:
        return False
    allow = list(domains) if domains is not None else trusted_gateway_domains()
    return any(_host_matches(host, str(d or "").lower().lstrip("."))
               for d in allow)


def validate_backup_gateway_urls(urls: Optional[Sequence[str]],
                                 domains: Optional[Sequence[str]] = None
                                 ) -> List[str]:
    """过滤备用网关列表：**只保留可信地址**（去重保序）。

    - 非字符串 / 空串 ⇒ 丢弃；
    - 协议不是 ``wss://`` / ``https://`` ⇒ 丢弃；
    - 主机不在信任域白名单 ⇒ 丢弃；
    - 重复项只保留第一次出现的。
    """
    allow = list(domains) if domains is not None else trusted_gateway_domains()
    kept: List[str] = []
    for raw in list(urls or []):
        text = str(raw or "").strip()
        if not text:
            continue
        if not is_trusted_gateway_url(text, allow):
            continue
        if text in kept:
            continue
        kept.append(text)
    return kept



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
    "DEFAULT_TRUSTED_GATEWAY_DOMAINS",
    "TRUSTED_GATEWAY_SCHEMES",
    "TRUSTED_GATEWAY_DOMAINS_ENV",
    "GatewayFailover",
    "is_trusted_gateway_url",
    "normalize_gateway_list",
    "to_heartbeat_url",
    "to_ws_url",
    "trusted_gateway_domains",
    "validate_backup_gateway_urls",
]
