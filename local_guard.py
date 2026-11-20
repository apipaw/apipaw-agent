"""
local_guard.py —— 阶段二十二 P1 · 本地面板的 **Host / Origin 双闸门**

为什么需要（审计 §5A.2 第 11、12 条）
------------------------------------
Agent 的本地 HTTP 服务（默认 ``127.0.0.1:52415``）同时提供：

* ``/ui``            —— 本地配置面板（单文件 HTML）
* ``/api/bind``      —— **写入 Node ID / Node Secret**（能改节点归属）
* ``/api/prefs`` / ``/api/mesh/prefs``
                      —— 写配置（含**备用网关地址**）
* ``/api/ollama/*``  —— 拉模型 / 切接单模型

它只监听回环地址，但**「只监听回环」不等于「只有本人能访问」**：

1. **DNS Rebinding**：攻击者把 ``evil.com`` 的 DNS 指向 ``127.0.0.1``，
   浏览器于是向 ``http://evil.com:52415/api/bind`` 发请求 ——
   目标 IP 是回环，**浏览器的同源策略拦不住**（同源看的是域名，不是 IP）。
   ⇒ 必须校验 **Host 头**（只认 ``127.0.0.1`` / ``localhost``）。
2. **跨站表单 / fetch**：恶意页面直接 ``fetch("http://127.0.0.1:52415/api/bind")``
   ⇒ 浏览器会带上 ``Origin: https://evil.com``。
   ⇒ 写端点必须校验 **Origin**（只认本面板自己的来源）。

两道闸门缺一不可：
* 只有 Host 校验 ⇒ 攻击者页面用 ``fetch`` 打 ``http://127.0.0.1:52415``
  （Host 头是 ``127.0.0.1``，合法！）—— 仍可 CSRF；
* 只有 Origin 校验 ⇒ 少数不发送 Origin 的老浏览器 / 某些跳转场景可绕过。

对外接口
--------
* :func:`allowed_hosts` —— 允许的 Host 头集合
* :func:`allowed_origins` —— 允许的 Origin 集合
* :func:`check_request_origin` —— 判定一次写请求的来源是否合法
"""

from __future__ import annotations

import os
from typing import Any, Dict, Iterable, List, Optional, Tuple

#: 永远信任的两个回环名
LOOPBACK_HOSTS = ("127.0.0.1", "localhost")

#: 允许追加 Host 的环境变量
EXTRA_HOSTS_ENV = "APIPAW_AGENT_ALLOWED_HOSTS"

#: 允许追加 Origin 的环境变量
EXTRA_ORIGINS_ENV = "APIPAW_AGENT_ALLOWED_ORIGINS"

#: 本面板的写端点（需要 Origin 校验）
LOCAL_WRITE_PATHS = (
    "/api/bind",
    "/api/prefs",
    "/api/mesh/prefs",
    "/api/ollama/pull",
    "/api/ollama/bind",
    "/api/ollama/evolve",
)

#: 通配监听地址（不是合法 Host）
_WILDCARD_HOSTS = ("0.0.0.0", "::", "[::]")


def _split_env(value: str) -> List[str]:
    return [x.strip() for x in str(value or "").split(",") if x.strip()]


def allowed_hosts(bind_host: str = "", bind_port: int = 0) -> List[str]:
    """允许的 ``Host`` 头集合（小写）。

    - 恒含 ``127.0.0.1`` / ``localhost``；
    - 若 Agent 绑在**具体**地址上（不是 ``0.0.0.0``），把该地址也放行 ——
      否则用户把 ``host`` 改成局域网 IP 后，面板自己都打不开；
    - ``APIPAW_AGENT_ALLOWED_HOSTS`` 可追加（英文逗号分隔）。
    """
    hosts: List[str] = []
    for item in list(LOOPBACK_HOSTS) + _split_env(os.environ.get(EXTRA_HOSTS_ENV, "")):
        text = str(item or "").strip().lower()
        if text and text not in hosts:
            hosts.append(text)
    bind = str(bind_host or "").strip().lower()
    if bind and bind not in _WILDCARD_HOSTS and bind not in hosts:
        hosts.append(bind)
    return hosts


def allowed_origins(bind_host: str = "", bind_port: int = 0) -> List[str]:
    """允许的 ``Origin`` 集合。

    - 恒含 ``http://127.0.0.1:<port>`` / ``http://localhost:<port>``
      （端口取**实际监听端口**，否则用户改了端口面板就自己把自己拒了）；
    - 若绑在具体地址上，加上对应 Origin；
    - ``APIPAW_AGENT_ALLOWED_ORIGINS`` 可追加。
    """
    port = int(bind_port or 0)
    origins: List[str] = []
    seeds = list(LOOPBACK_HOSTS)
    bind = str(bind_host or "").strip().lower()
    if bind and bind not in _WILDCARD_HOSTS:
        seeds.append(bind)
    for host in seeds:
        text = "http://%s:%d" % (host, port) if port else "http://%s" % host
        if text not in origins:
            origins.append(text)
    for item in _split_env(os.environ.get(EXTRA_ORIGINS_ENV, "")):
        text = item.rstrip("/").lower()
        if text and text not in origins:
            origins.append(text)
    return origins


def check_request_origin(headers: Dict[str, Any],
                          origins: Optional[Iterable[str]] = None
                          ) -> Tuple[bool, str]:
    """判定一次**写请求**的来源是否合法。

    返回 ``(ok, reason)``；``reason`` 只用于日志与错误文案。

    判定顺序：

    1. ``Sec-Fetch-Site: cross-site`` ⇒ **拒绝**（浏览器跨站请求的
       权威标记，即便 Origin 缺失也能抓住）；
    2. ``Origin`` 存在 ⇒ 必须命中白名单，否则 **拒绝**；
    3. ``Origin`` 缺失 ⇒ **放行**。

    ⚠️ 为什么 Origin 缺失放行：本地 CLI / 测试客户端 / 桌面自动化
    （``curl -X POST http://127.0.0.1:52415/api/prefs``）根本不会发 Origin；
    而浏览器发起的跨站攻击**一定**会带 Origin（或 ``Sec-Fetch-Site``）。
    把「无 Origin」一律拒绝 ⇒ 正常用户用命令行改配置直接坏掉，
    换来的却是对**非浏览器**攻击面的零增益。
    """
    raw = headers or {}
    lowered = {}
    for key, value in raw.items():
        try:
            lowered[str(key).lower()] = str(value)
        except Exception:  # noqa: BLE001 - 头解析失败按缺省处理
            continue

    site = (lowered.get("sec-fetch-site") or "").strip().lower()
    if site == "cross-site":
        return False, "cross_site_request"

    origin = (lowered.get("origin") or "").strip().rstrip("/")
    if not origin:
        return True, "no_origin"

    allow = [str(o or "").rstrip("/").lower()
             for o in (origins if origins is not None else allowed_origins())]
    if origin.lower() in allow:
        return True, "origin_allowed"
    return False, "origin_not_allowed"


__all__ = [
    "EXTRA_HOSTS_ENV",
    "EXTRA_ORIGINS_ENV",
    "LOCAL_WRITE_PATHS",
    "LOOPBACK_HOSTS",
    "allowed_hosts",
    "allowed_origins",
    "check_request_origin",
]
