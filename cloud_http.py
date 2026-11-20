"""
cloud_http.py — 云端 HTTP 请求的统一出口（v1.0.19 · 真机 Bug C 根治）

问题
----
Agent 访问云端（``cloud.apipaw.com``）时，云端在 **Cloudflare** 边缘之后。
Cloudflare 的 WAF / Bot 规则对 Python 标准库默认 UA 有一道明确拦截：

    User-Agent: Python-urllib/3.12   →  HTTP Error 403: Forbidden

真机表现为「读取云端推荐列表」与「读取黄金记忆」两块面板直接报错；
同时国内直连链路抖动会让 TLS 握手偶发超时，若**同步阻塞**在事件循环里，
前端请求本机端口就会表现为 ``Failed to fetch``。

修复
----
本模块把「访问云端」的所有细节收敛到一处：

1. **显式 User-Agent**：不再让 urllib 用默认的 ``Python-urllib/x.y``；
   统一带 ``Mozilla/5.0 ... AppleWebKit/537.36 APIPawAgent/<版本>``。
   ⚠️ **不要**改回默认 UA —— Cloudflare 边缘会直接 403（真机 Bug C 根因，
   2026-11-19 现场复现：同一 URL 换 UA 即 200）。
2. **显式 Accept**：JSON 端点带 ``application/json``，纯文本端点带
   ``text/plain``。
3. **短超时 + 退避重试**：面板类请求用 **4s 短超时**（宁可失败也不要
   拖住本地面板）；链路抖动（``URLError`` / ``OSError``，含 TLS 握手
   超时）与 CF 的 ``403`` / ``429`` / ``5xx`` 会退避重试；真正的业务错误
   （``400`` / ``401`` / ``404`` …）**不重试**，立刻抛出让上层如实回显。
4. **内存缓存兜底**：``fetch_cloud_json_cached`` 记住最近一次成功结果，
   网络出问题时**回落到缓存**并标注 ``stale=True``，面板不至于整块变空。

设计取舍
--------
- **零新增依赖**：只用标准库 ``urllib`` + ``json`` + ``time``。
- **不吞异常**：重试耗尽后把最后一个异常原样抛出，由调用方决定怎么
  回显给用户；本模块只负责「多试几次」「带上正确的头」「记住上一次成功」。
- **可注入**：``opener`` 参数可替换 ``urllib.request.urlopen``，
  单测据此做**零网络**验证。

兼容���
------
源站 Python 3.6.8 也能解析本文件（**不使用** ``from __future__ import
annotations``，也不使用 f-string）。
"""

import json
import time
import urllib.error
import urllib.request
from typing import Any, Callable, Dict, Optional, Tuple

#: 合规 UA —— 以 Mozilla/5.0 开头（Cloudflare 只对「默认 Python UA」这种
#: 明显自动化标识硬拦），追加 APIPawAgent 版本号便于服务端排障。
CLOUD_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 APIPawAgent/1.0.20"
)

#: 客户端标识头（与 UA 分开，便于服务端按版本做灰度与排障）
CLIENT_HEADER_NAME = "X-APIPaw-Client"
CLIENT_HEADER_VALUE = "APIPawAgent/1.0.20"

#: 面板类请求的超时（秒）。**短超时是硬要求**：宁可这一块显示「暂时取不到」，
#: 也不能让本机面板的其它接口（/api/status、/api/ollama/tags）跟着卡。
DEFAULT_TIMEOUT_SEC = 4.0

#: 默认重试次数（不含首次请求）。
DEFAULT_RETRIES = 2

#: 触发重试的 HTTP 状态码。
#:
#: - ``403``：Cloudflare 边缘规则误伤（带上正确 UA 后极少发生，
#:   但边缘节点规则并非全局一致，值得再试一次）。
#: - ``429``：限流，退避后大概率放行。
#: - ``5xx``：源站/网关瞬时故障。
#:
#: 其余 4xx（400/401/404/422 …）是**业务错误**，重试无意义，直接抛。
RETRY_STATUS_CODES = (403, 429, 500, 502, 503, 504)

#: 重试之间的基础退避（秒），按次数指数增长。
_BACKOFF_BASE_SEC = 0.4
_BACKOFF_MAX_SEC = 2.0

#: 缓存有效期（秒）。超过后仍可作为**兜底**返回（标注 stale）。
CACHE_TTL_SEC = 300.0

#: 进程内缓存：url → (值, 落库时间戳)
_CACHE: Dict[str, Tuple[Any, float]] = {}


def cloud_headers(accept: str = "application/json") -> Dict[str, str]:
    """构造访问云端时的标准请求头（UA + Accept + 客户端标识）。"""
    return {
        "User-Agent": CLOUD_USER_AGENT,
        "Accept": accept,
        CLIENT_HEADER_NAME: CLIENT_HEADER_VALUE,
    }


def _sleep_backoff(attempt: int) -> None:
    """第 ``attempt`` 次重试前的退避等待（0.4s → 0.8s …，封顶 2s）。"""
    delay = min(_BACKOFF_BASE_SEC * (2 ** max(0, attempt - 1)), _BACKOFF_MAX_SEC)
    time.sleep(delay)


def _should_retry_status(status: int) -> bool:
    """该 HTTP 状态码是否值得重试。"""
    return status in RETRY_STATUS_CODES


def fetch_cloud_text(
    url: str,
    accept: str = "text/plain",
    timeout: float = DEFAULT_TIMEOUT_SEC,
    retries: int = DEFAULT_RETRIES,
    opener: Optional[Callable[..., Any]] = None,
) -> str:
    """
    GET 一个云端 URL 并返回文本。

    :param url: 完整 URL。
    :param accept: ``Accept`` 头。
    :param timeout: 单次请求超时（秒）。
    :param retries: 失败后的重试次数（不含首次）。
    :param opener: 可注入的 ``urlopen`` 实现（单测用；默认
        ``urllib.request.urlopen``）。
    :raises urllib.error.HTTPError: 业务错误（4xx 非可重试）立即抛出。
    :raises urllib.error.URLError: 链路错误重试耗尽后抛出。
    """
    do_open = opener or urllib.request.urlopen
    attempts = max(1, int(retries) + 1)
    last_exc: Optional[BaseException] = None

    for attempt in range(1, attempts + 1):
        request = urllib.request.Request(url, headers=cloud_headers(accept))
        try:
            with do_open(request, timeout=timeout) as resp:
                raw = resp.read()
            if isinstance(raw, bytes):
                return raw.decode("utf-8", "replace")
            return str(raw)
        except urllib.error.HTTPError as exc:
            last_exc = exc
            if not _should_retry_status(exc.code) or attempt >= attempts:
                raise
        except (urllib.error.URLError, OSError) as exc:
            # 链路层失败：TLS 握手超时 / 连接重置 / DNS 抖动
            last_exc = exc
            if attempt >= attempts:
                raise
        _sleep_backoff(attempt)

    # 理论不可达（循环内必定 return 或 raise），保底抛最后一个异常
    if last_exc is not None:
        raise last_exc
    raise urllib.error.URLError("云端请求失败")


def fetch_cloud_json(
    url: str,
    timeout: float = DEFAULT_TIMEOUT_SEC,
    retries: int = DEFAULT_RETRIES,
    opener: Optional[Callable[..., Any]] = None,
) -> Dict[str, Any]:
    """
    GET 一个云端 JSON 端点并返回字典。

    解析失败时返回空字典（上层据此显示「云端返回格式异常」），
    但 HTTP 层错误照常抛出，由上层回显人话。
    """
    text = fetch_cloud_text(
        url,
        accept="application/json",
        timeout=timeout,
        retries=retries,
        opener=opener,
    )
    return _parse_json_object(text)


def _parse_json_object(text: str) -> Dict[str, Any]:
    """把文本解析成 dict；解析失败返回空 dict（不抛）。"""
    try:
        obj = json.loads(text)
    except ValueError:
        return {}
    return obj if isinstance(obj, dict) else {}


def fetch_cloud_json_cached(
    url: str,
    timeout: float = DEFAULT_TIMEOUT_SEC,
    retries: int = DEFAULT_RETRIES,
    opener: Optional[Callable[..., Any]] = None,
) -> Tuple[Dict[str, Any], bool]:
    """
    带**内存缓存兜底**的 JSON 拉取。

    返回 ``(数据, 是否为过期兜底)``：

    - 成功 ⇒ ``(新数据, False)``，同时写入缓存；
    - 失败且缓存里有旧值 ⇒ ``(旧值, True)``（面板仍能显示内容，
      只是提示「数据可能不是最新的」）；
    - 失败且无缓存 ⇒ 异常照常抛给调用方。

    ⚠️ 缓存是**进程内**的（Agent 单进程长期运行），重启即失效——
    这是刻意的：缓存只是「网络抖动时不白屏」，不是持久化存储。
    """
    try:
        data = fetch_cloud_json(url, timeout=timeout, retries=retries, opener=opener)
    except Exception:
        cached = _CACHE.get(url)
        if cached is not None:
            return cached[0], True
        raise
    _CACHE[url] = (data, time.time())
    return data, False


def cache_age_sec(url: str) -> Optional[float]:
    """返回某 URL 的缓存年龄（秒）；无缓存 ⇒ ``None``。"""
    cached = _CACHE.get(url)
    if cached is None:
        return None
    return max(0.0, time.time() - cached[1])
