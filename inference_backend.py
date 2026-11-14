"""
inference_backend.py — 统一推理后端抽象层（阶段六 P0 · 模块 A）

背景
----
阶段一~五把「钱怎么算、怎么分、怎么派、怎么审」做完了，但**数据面（推理）
从不存在**：节点收到派单后跑的是 ``main.py`` 里的占位文本。本模块是阶段六
「真实推理引擎接入」的第一块基石 —— 把**推理引擎**从节点逻辑里抽出来，
让 Agent 具备真正跑模型的能力。

设计要点（D1 拍板：Ollama 优先 + 统一抽象层）
------------------------------------------------
1. **统一接口**：所有后端实现同一个 ``InferenceBackend`` 抽象类
   （``generate`` / ``list_models`` / ``is_available``），
   上层（``ws_tunnel`` / ``main``）只认接口，不认具体引擎。
2. **纯标准库**：只用 ``urllib`` + ``json`` + ``threading``，**零新增依赖**。
   （红线：发行包不引入任何新的生产依赖。）
3. **绝不回落模拟回答**：后端不可用时抛 ``BackendUnavailable``
   （错误码 ``backend_unavailable``），由调用方明确报错 ——
   **绝不能**悄悄回落到占位文本让云端以为"推理成功"。
4. **真实 usage 优先**：后端回报的真实 prompt/completion token 随分片一路带到
   ``RESPONSE_DONE``，云端据此精确计费（A3）。

后端矩阵
--------
============  ==========================================  ================
后端          协议                                       状态
============  ==========================================  ================
``ollama``    ``POST /api/chat``（NDJSON 流）+ ``/api/tags``  ✅ P0 已实现
``vllm``      OpenAI 兼容 ``POST /v1/chat/completions``（SSE）  ⏳ P1 预留位
``llamacpp``  OpenAI 兼容 ``POST /v1/chat/completions``（SSE）  ⏳ P1 预留位
============  ==========================================  ================

> ``vllm`` / ``llamacpp`` 已按 D1 在抽象层里**预留好同接口实现位**
> （类、错误语义、配置项、工厂分支都在），P1 只需补 ``generate`` 的实现体，
> **上层无需改动**。
"""

from __future__ import annotations

import json
import threading
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from typing import Any, AsyncIterator, Dict, List, Optional, Sequence

# ── 后端标识 ────────────────────────────────────────────────
BACKEND_AUTO = "auto"
BACKEND_OLLAMA = "ollama"
BACKEND_VLLM = "vllm"
BACKEND_LLAMACPP = "llamacpp"

SUPPORTED_BACKENDS = (BACKEND_AUTO, BACKEND_OLLAMA, BACKEND_VLLM, BACKEND_LLAMACPP)

DEFAULT_OLLAMA_BASE_URL = "http://127.0.0.1:11434"
DEFAULT_TIMEOUT_SEC = 60.0

# ``auto`` 模式下探测后端的缓存时长：Ollama 可能比节点晚启动，
# 缓存太长会让节点「永远发现不了后端」，太短则失去热插拔能力。
AUTO_PROBE_TTL_SEC = 30.0


class BackendError(RuntimeError):
    """后端调用失败（HTTP 非 2xx / 响应体不可解析 / 协议不符）。"""

    code = "backend_error"


class BackendUnavailable(BackendError):
    """后端**不存在或连不上**。

    ⚠️ 这是阶段六的**红线错误**：节点绝不能因此回落到模拟回答 ——
    云端必须看到明确失败（``backend_unavailable``），否则会出现
    "付费买到了占位文本"的静默欺诈。
    """

    code = "backend_unavailable"


@dataclass
class BackendUsage:
    """后端回报的**真实** token 用量。"""

    prompt_tokens: int = 0
    completion_tokens: int = 0

    @property
    def total_tokens(self) -> int:
        return int(self.prompt_tokens) + int(self.completion_tokens)

    def to_dict(self) -> Dict[str, int]:
        return {
            "prompt_tokens": int(self.prompt_tokens),
            "completion_tokens": int(self.completion_tokens),
            "total_tokens": self.total_tokens,
        }


@dataclass
class GenerationChunk:
    """一次生成的分片。

    ``usage`` 只在**结束分片**上有值（后端回报的真实用量）；
    ``done=True`` 表示这是最后一片。
    """

    text: str = ""
    usage: Optional[BackendUsage] = None
    done: bool = False


def normalize_model_key(name: str) -> str:
    """模型名**归一化键**：去掉所有非字母数字字符并转小写。

    例：``qwen-2.5-72b`` / ``qwen2.5:72b`` / ``Qwen2.5-72B`` ⇒ ``qwen2572b``。

    为什么需要：云端目录里的模型 id（``qwen-2.5-72b``）与本地推理引擎的
    tag（``qwen2.5:72b``）是**两套命名体系**，直接字符串比较永远对不上。
    归一化后两者可比 —— 云端据此判断"这台节点装没装这个模型"，
    节点据此把云端请求解析成本地 tag。
    """
    text = str(name or "").strip().lower()
    return "".join(ch for ch in text if ch.isalnum())


def resolve_model(requested: str, available: Sequence[str],
                  default_model: str = "") -> str:
    """把云端请求的模型 id 解析成本地**真实存在**的模型 tag。

    解析顺序（先精确后模糊，绝不臆造）：
    1. **精确命中** —— 请求名在 ``available`` 里 ⇒ 直接用它；
    2. **归一化命中** —— 归一化后相等（``qwen-2.5-72b`` ↔ ``qwen2.5:72b``）⇒ 用本地 tag；
    3. **回落到默认模型** —— 配置了 ``default_model``（``backend_model``）且它确实在本地 ⇒ 用它
       （云端要的模型本机没有，但节点被配置成"专跑这个模型"，仍比直接失败好）；
    4. **本机只有一个模型** ⇒ 用它（无从选择，且这多半就是用户想跑的）；
    5. 都不满足 ⇒ 抛 ``BackendError``（**绝不**拿不相干的模型顶替）。

    ⚠️ 第 5 步是红线：宁可明确失败，也不能让云端以为"跑的是它点的模型"，
    实际上跑的是另一个 —— 那是**静默欺诈**，比失败严重得多。
    """
    wanted = str(requested or "").strip()
    options = [str(m).strip() for m in (available or []) if str(m).strip()]

    if wanted:
        if wanted in options:
            return wanted
        key = normalize_model_key(wanted)
        for opt in options:
            if normalize_model_key(opt) == key:
                return opt

    fallback = str(default_model or "").strip()
    if fallback and fallback in options:
        return fallback
    if len(options) == 1:
        return options[0]
    if not options:
        raise BackendError("本机没有可用模型（后端未装载任何模型）")
    raise BackendError("本机没有云端要求的模型：%s（本地可用：%s）"
                        % (wanted or "(未指定)", ", ".join(options[:8])))


class InferenceBackend:
    """推理后端抽象基类 —— 所有后端的**统一契约**。

    子类必须实现：
    - ``name``：后端标识（写入配置与上报帧）
    - ``is_available()``：能否连通（不抛异常，只返回布尔）
    - ``list_models()``：本地可用模型清单
    - ``generate(...)``：异步生成器，产出 ``GenerationChunk``
    """

    name = "base"

    def __init__(self, base_url: str = "", timeout_sec: float = DEFAULT_TIMEOUT_SEC,
                 model: str = "") -> None:
        self.base_url = (base_url or "").rstrip("/")
        self.timeout_sec = float(timeout_sec or DEFAULT_TIMEOUT_SEC)
        self.model = model or ""

    # ── 契约 ────────────────────────────────────────────────
    def is_available(self) -> bool:
        raise NotImplementedError

    def list_models(self) -> List[str]:
        raise NotImplementedError

    async def generate(self, model: str, messages: Sequence[Dict[str, Any]],
                       temperature: float = 1.0, top_p: float = 1.0,
                       max_tokens: Optional[int] = None) -> AsyncIterator[GenerationChunk]:
        """流式生成。产出 ``GenerationChunk``，最后一个分片带真实 usage。"""
        raise NotImplementedError
        yield  # pragma: no cover - 仅用于满足异步生成器协议

    # ── 工具 ────────────────────────────────────────────────
    def _url(self, path: str) -> str:
        return "%s%s" % (self.base_url, path)


class OllamaBackend(InferenceBackend):
    """Ollama 后端（P0 首个落地实现）。

    - 生成：``POST /api/chat``，请求体带 ``stream: true``，
      响应是 **NDJSON**（一行一个 JSON，末行 ``done: true`` 且带
      ``prompt_eval_count`` / ``eval_count`` 真实 token 数）。
    - 模型清单：``GET /api/tags``。

    ⚠️ Ollama 的 ``/api/chat`` **没有** 非流式模式可选（``stream:false``
    会一次性返回整段），但那会让节点拿不到"边生成边回传"的真流式体验，
    因此本实现**始终**走流式。
    """

    name = BACKEND_OLLAMA

    def __init__(self, base_url: str = DEFAULT_OLLAMA_BASE_URL,
                 timeout_sec: float = DEFAULT_TIMEOUT_SEC, model: str = "") -> None:
        super().__init__(base_url or DEFAULT_OLLAMA_BASE_URL, timeout_sec, model)

    # ── HTTP 底座 ──────────────────────────────────────────
    def _http(self, path: str, payload: Optional[Dict[str, Any]] = None,
              method: str = "GET", timeout: Optional[float] = None) -> str:
        data = None
        if payload is not None:
            data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        req = urllib.request.Request(
            self._url(path), data=data, method=method,
            headers={"Content-Type": "application/json",
                     "Accept": "application/json, application/x-ndjson"},
        )
        try:
            with urllib.request.urlopen(req, timeout=timeout or self.timeout_sec) as resp:
                return resp.read().decode("utf-8", "replace")
        except urllib.error.HTTPError as exc:  # 后端在、但请求不合法
            detail = ""
            try:
                detail = exc.read().decode("utf-8", "replace")[:200]
            except Exception:  # noqa: BLE001 - 读错误体失败不影响主结论
                detail = ""
            raise BackendError("Ollama 返回 HTTP %s %s" % (exc.code, detail)) from exc
        except (urllib.error.URLError, OSError) as exc:  # 连不上
            raise BackendUnavailable("无法连接 Ollama（%s）：%s" % (self.base_url, exc)) from exc

    # ── 契约实现 ────────────────────────────────────────────
    def is_available(self) -> bool:
        try:
            self._http("/api/tags", timeout=min(5.0, self.timeout_sec))
            return True
        except BackendError:
            return False

    def list_models(self) -> List[str]:
        raw = self._http("/api/tags")
        try:
            obj = json.loads(raw)
        except ValueError as exc:
            raise BackendError("Ollama /api/tags 返回的不是合法 JSON") from exc
        names: List[str] = []
        for item in (obj.get("models") or []):
            if not isinstance(item, dict):
                continue
            name = item.get("name") or item.get("model")
            if name:
                names.append(str(name))
        return names

    async def generate(self, model: str, messages: Sequence[Dict[str, Any]],
                       temperature: float = 1.0, top_p: float = 1.0,
                       max_tokens: Optional[int] = None) -> AsyncIterator[GenerationChunk]:
        target = model or self.model
        if not target:
            raise BackendError("未指定模型名（配置 backend_model 或派单载荷里的 model）")
        options: Dict[str, Any] = {"temperature": float(temperature)}
        if top_p and float(top_p) != 1.0:
            options["top_p"] = float(top_p)
        if max_tokens and int(max_tokens) > 0:
            options["num_predict"] = int(max_tokens)
        body = {
            "model": target,
            "messages": [dict(m) for m in messages],
            "stream": True,
            "options": options,
        }

        import asyncio  # 局部导入：仅在真正生成时才需要事件循环

        loop = asyncio.get_event_loop()
        queue: "asyncio.Queue[Any]" = asyncio.Queue()
        _DONE = object()

        def _worker() -> None:
            """工作线程：逐行读 NDJSON，把分片塞进事件循环队列。"""
            try:
                req = urllib.request.Request(
                    self._url("/api/chat"),
                    data=json.dumps(body, ensure_ascii=False).encode("utf-8"),
                    method="POST",
                    headers={"Content-Type": "application/json",
                             "Accept": "application/x-ndjson"},
                )
                with urllib.request.urlopen(req, timeout=self.timeout_sec) as resp:
                    for raw_line in resp:
                        line = raw_line.decode("utf-8", "replace").strip()
                        if not line:
                            continue
                        try:
                            obj = json.loads(line)
                        except ValueError:
                            continue  # 非 JSON 行直接跳过（Ollama 偶尔混入空行）
                        if not isinstance(obj, dict):
                            continue
                        if obj.get("error"):
                            raise BackendError("Ollama 报错：%s" % obj.get("error"))
                        message = obj.get("message") or {}
                        content = message.get("content") or ""
                        done = bool(obj.get("done"))
                        usage = None
                        if done:
                            usage = BackendUsage(
                                prompt_tokens=int(obj.get("prompt_eval_count") or 0),
                                completion_tokens=int(obj.get("eval_count") or 0),
                            )
                        loop.call_soon_threadsafe(
                            queue.put_nowait,
                            GenerationChunk(text=content, usage=usage, done=done),
                        )
                        if done:
                            break
            except BackendError as exc:
                loop.call_soon_threadsafe(queue.put_nowait, exc)
            except (urllib.error.URLError, OSError) as exc:
                loop.call_soon_threadsafe(
                    queue.put_nowait, BackendUnavailable("Ollama 连接中断：%s" % exc))
            except Exception as exc:  # noqa: BLE001 - 兜底，绝不让线程静默死掉
                loop.call_soon_threadsafe(queue.put_nowait, BackendError(str(exc)))
            finally:
                loop.call_soon_threadsafe(queue.put_nowait, _DONE)

        threading.Thread(target=_worker, name="ollama-gen", daemon=True).start()

        while True:
            item = await queue.get()
            if item is _DONE:
                break
            if isinstance(item, Exception):
                raise item
            yield item


class ReservedOpenAICompatBackend(InferenceBackend):
    """OpenAI 兼容后端的**预留位**（vLLM / llama.cpp 共用）。

    D1 拍板：这两个后端在 P1 补实现。接口、错误语义、配置项、工厂分支
    已经在 P0 铺好 —— P1 只需实现 ``generate``（解析 ``data: `` SSE 行），
    **上层（``ws_tunnel`` / ``main``）一行都不用改**。

    当前实现**明确报错**，绝不静默回落模拟回答。
    """

    name = "reserved"

    def is_available(self) -> bool:
        return False

    def list_models(self) -> List[str]:
        return []

    async def generate(self, model: str, messages: Sequence[Dict[str, Any]],
                       temperature: float = 1.0, top_p: float = 1.0,
                       max_tokens: Optional[int] = None) -> AsyncIterator[GenerationChunk]:
        raise BackendUnavailable(
            "后端 %s 尚未实现（阶段六 P1 补：OpenAI 兼容 SSE 解析）" % self.name)
        yield  # pragma: no cover - 仅用于满足异步生成器协议


class VLLMBackend(ReservedOpenAICompatBackend):
    """vLLM 预留位（高端多卡节点）。"""

    name = BACKEND_VLLM


class LlamaCppBackend(ReservedOpenAICompatBackend):
    """llama.cpp 预留位（无 GPU 老机器）。"""

    name = BACKEND_LLAMACPP


# ── 工厂 & 单例 ─────────────────────────────────────────────
_backend_lock = threading.Lock()
_backend_instance: Optional[InferenceBackend] = None
_backend_key: str = ""
_backend_probed_at: float = 0.0


def _cfg_value(cfg: Any, name: str, default: Any = "") -> Any:
    """从配置对象取值（鸭子类型，不强依赖 ``agent_config``）。"""
    if cfg is None:
        return default
    value = getattr(cfg, name, None)
    if value is None and isinstance(cfg, dict):
        value = cfg.get(name)
    return default if value is None else value


def build_backend(cfg: Any = None) -> Optional[InferenceBackend]:
    """按配置构造后端实例（**不**做连通性探测）。

    - ``auto``：构造 Ollama 后端（是否可用由 ``is_available`` 判定）；
    - ``ollama``：显式 Ollama；
    - ``vllm`` / ``llamacpp``：预留实现位。

    返回 ``None`` 表示配置非法。
    """
    kind = str(_cfg_value(cfg, "inference_backend", BACKEND_AUTO) or BACKEND_AUTO).strip().lower()
    base_url = str(_cfg_value(cfg, "backend_base_url", "") or "").strip()
    model = str(_cfg_value(cfg, "backend_model", "") or "").strip()
    try:
        timeout = float(_cfg_value(cfg, "backend_timeout_sec", DEFAULT_TIMEOUT_SEC)
                        or DEFAULT_TIMEOUT_SEC)
    except (TypeError, ValueError):
        timeout = DEFAULT_TIMEOUT_SEC

    if kind in (BACKEND_AUTO, BACKEND_OLLAMA):
        return OllamaBackend(base_url or DEFAULT_OLLAMA_BASE_URL, timeout, model)
    if kind == BACKEND_VLLM:
        return VLLMBackend(base_url, timeout, model)
    if kind == BACKEND_LLAMACPP:
        return LlamaCppBackend(base_url, timeout, model)
    return None


def get_backend(cfg: Any = None, force_refresh: bool = False) -> Optional[InferenceBackend]:
    """获取当前生效的推理后端（带缓存）。

    - 配置为 ``auto`` 时会**探测** Ollama：探不到 ⇒ 返回 ``None``
      （调用方据此回 ``backend_unavailable``，绝不回落模拟回答）。
    - 探测结果缓存 ``AUTO_PROBE_TTL_SEC`` 秒：Ollama 晚于节点启动时，
      节点能在 TTL 内自动发现它，无需重启。
    """
    global _backend_instance, _backend_key, _backend_probed_at

    kind = str(_cfg_value(cfg, "inference_backend", BACKEND_AUTO) or BACKEND_AUTO).strip().lower()
    base_url = str(_cfg_value(cfg, "backend_base_url", "") or "").strip()
    model = str(_cfg_value(cfg, "backend_model", "") or "").strip()
    try:
        timeout = float(_cfg_value(cfg, "backend_timeout_sec", DEFAULT_TIMEOUT_SEC)
                        or DEFAULT_TIMEOUT_SEC)
    except (TypeError, ValueError):
        timeout = DEFAULT_TIMEOUT_SEC
    key = "%s|%s|%s|%.1f" % (kind, base_url, model, timeout)

    now = time.time()
    with _backend_lock:
        if not force_refresh and _backend_instance is not None and _backend_key == key:
            if kind != BACKEND_AUTO or (now - _backend_probed_at) < AUTO_PROBE_TTL_SEC:
                return _backend_instance
        backend = build_backend(cfg)
        if backend is None:
            _backend_instance = None
            _backend_key = key
            _backend_probed_at = now
            return None
        if kind == BACKEND_AUTO and not backend.is_available():
            # auto 模式探不到 ⇒ 本节点**没有**推理引擎（返回 None，
            # 上层必须回 backend_unavailable）。
            _backend_instance = None
            _backend_key = key
            _backend_probed_at = now
            return None
        _backend_instance = backend
        _backend_key = key
        _backend_probed_at = now
        return backend


def reset_backend_cache() -> None:
    """清空后端缓存（测试用 / 热更新配置用）。"""
    global _backend_instance, _backend_key, _backend_probed_at
    with _backend_lock:
        _backend_instance = None
        _backend_key = ""
        _backend_probed_at = 0.0


def describe_backend(cfg: Any = None) -> Dict[str, Any]:
    """给 ``/health`` 用的后端状态快照（不含任何凭证）。"""
    backend = get_backend(cfg)
    kind = str(_cfg_value(cfg, "inference_backend", BACKEND_AUTO) or BACKEND_AUTO).lower()
    models: List[str] = []
    if backend is not None:
        try:
            models = backend.list_models()
        except BackendError:
            models = []
    return {
        "configured": kind,
        "active": backend.name if backend is not None else "",
        "available": backend is not None,
        "base_url": backend.base_url if backend is not None else "",
        "models": models,
    }


__all__ = [
    "BACKEND_AUTO",
    "BACKEND_OLLAMA",
    "BACKEND_VLLM",
    "BACKEND_LLAMACPP",
    "SUPPORTED_BACKENDS",
    "DEFAULT_OLLAMA_BASE_URL",
    "BackendError",
    "BackendUnavailable",
    "BackendUsage",
    "GenerationChunk",
    "InferenceBackend",
    "OllamaBackend",
    "VLLMBackend",
    "LlamaCppBackend",
    "build_backend",
    "get_backend",
    "reset_backend_cache",
    "describe_backend",
    "normalize_model_key",
    "resolve_model",
]
