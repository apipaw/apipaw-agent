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
``vllm``      OpenAI 兼容 ``POST /v1/chat/completions``（SSE）  ✅ P3 已实现
``llamacpp``  OpenAI 兼容 ``POST /v1/chat/completions``（SSE）  ✅ P3 已实现
============  ==========================================  ================

> ``vllm`` / ``llamacpp`` 共用 ``OpenAICompatBackend`` 的实现体
> （阶段六 P3 · 模块 A2）—— SSE 解析、真实 usage、断流语义与 Ollama 后端
> **完全同构**，上层（``ws_tunnel`` / ``main``）一行都不用改。
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
# 阶段六 P3 · 模块 A2：OpenAI 兼容后端的默认地址。
#   vLLM    —— ``vllm serve`` 默认监听 8000；
#   llama.cpp —— ``llama-server`` 默认监听 8080。
DEFAULT_VLLM_BASE_URL = "http://127.0.0.1:8000"
DEFAULT_LLAMACPP_BASE_URL = "http://127.0.0.1:8080"
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


class BackendStreamBroken(BackendError):
    """推理**跑到一半**后端挂了（阶段六 P2 · A4 断流语义）。

    与 ``BackendUnavailable`` 的区别：

    - ``BackendUnavailable``：开跑前就失败，一个字都没产出；
    - ``BackendStreamBroken``：已经吐了分片，连接却中途断了。

    为什么必须区分：云端按 D6 决策，**断流的单不计费**（没跑完的服务不收费），
    而"压根没跑起来"要按失败重试/换节点处理。两者混为一谈会让云端
    把"半个答案"当成完整答案计费，或反过来把可重试的失败当成断流。
    """

    code = "backend_stream_broken"


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
    3. **本机只有一个模型** ⇒ 用它（无从选择，且这多半就是用户想跑的）；
    4. **云端未指定模型** ⇒ 用配置的 ``default_model``（``backend_model``）；
    5. 都不满足 ⇒ 抛 ``BackendError``（**绝不**拿不相干的模型顶替）。

    ⚠️ 第 5 步是红线：宁可明确失败（网关据此换节点重试），也不能让云端以为
    "跑的是它点的模型"，实际上跑的是另一个 —— 那是**静默欺诈**，比失败严重得多。
    因此当云端**明确点名**了模型而本机没有时，**不**回落到 ``default_model``
    （盲派单 v2 下节点能从 ``model_cipher`` 解出真实模型名，此时回落＝顶替）。
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
        if len(options) == 1:
            return options[0]
        if not options:
            raise BackendError("本机没有可用模型（后端未装载任何模型）")
        raise BackendError("本机没有云端要求的模型：%s（本地可用：%s）"
                            % (wanted, ", ".join(options[:8])))

    # 云端未点名 ⇒ 用配置里的默认模型
    fallback = str(default_model or "").strip()
    if fallback and fallback in options:
        return fallback
    if len(options) == 1:
        return options[0]
    if not options:
        raise BackendError("本机没有可用模型（后端未装载任何模型）")
    raise BackendError("云端未指定模型，且本机有多个模型可选（本地可用：%s）"
                        % ", ".join(options[:8]))


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
            saw_done = False
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
                            saw_done = True
                            break
                # ⚠️ 阶段六 P2 · A4：**没收到 done 帧就断了** ⇒ 断流。
                #    少了这一句，连接被掐断时会被当成"正常结束"，
                #    隧道会回 RESPONSE_DONE，云端就会把**半个答案**当完整答案计费。
                if not saw_done:
                    raise BackendStreamBroken(
                        "Ollama 流中途断开（未收到 done 帧）")
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


class OpenAICompatBackend(InferenceBackend):
    """OpenAI 兼容后端（vLLM / llama.cpp 共用实现，阶段六 P3 · 模块 A2）。

    协议：
    - 生成：``POST /v1/chat/completions``，请求体带 ``stream: true``，
      响应是 **SSE**（每行 ``data: {json}``，末尾 ``data: [DONE]``）；
    - 模型清单：``GET /v1/models``（返回 ``{"data": [{"id": ...}, ...]}``）。

    与 Ollama 后端**完全同构**的三条语义（上层零改动）：

    1. **真实 usage 优先**：SSE 帧里的 ``usage``（需要后端支持
       ``stream_options.include_usage``）原样带给云端精确计费；
       没有就退回估算（由云端处理，节点不编造数字）。
    2. **断流可辨**：既没收到 ``data: [DONE]``、也没有 ``finish_reason``，
       就断了 ⇒ ``BackendStreamBroken``（阶段六 P2 · A4），
       云端据此免单。绝不把半个答案当完整答案。
    3. **绝不回落模拟回答**：连不上 ⇒ ``BackendUnavailable``。

    ⚠️ 兼容性细节：部分 llama.cpp 版本不认 ``stream_options`` 字段，
    会对请求体直接回 400。实现里做了**一次**降级重试
    （去掉 ``stream_options`` 再试），避免"字段不认 ⇒ 整个节点不可用"。
    """

    name = "openai_compat"

    def __init__(self, base_url: str = "", timeout_sec: float = DEFAULT_TIMEOUT_SEC,
                 model: str = "", api_key: str = "") -> None:
        super().__init__(base_url, timeout_sec, model)
        self.api_key = str(api_key or "").strip()

    # ── HTTP 底座 ──────────────────────────────────────────
    def _headers(self) -> Dict[str, str]:
        headers = {"Content-Type": "application/json",
                   "Accept": "text/event-stream, application/json"}
        if self.api_key:
            headers["Authorization"] = "Bearer %s" % self.api_key
        return headers

    def _http_get_json(self, path: str, timeout: Optional[float] = None) -> Any:
        req = urllib.request.Request(self._url(path), method="GET",
                                     headers=self._headers())
        try:
            with urllib.request.urlopen(req, timeout=timeout or self.timeout_sec) as resp:
                raw = resp.read().decode("utf-8", "replace")
        except urllib.error.HTTPError as exc:
            detail = ""
            try:
                detail = exc.read().decode("utf-8", "replace")[:200]
            except Exception:  # noqa: BLE001
                detail = ""
            raise BackendError("%s 返回 HTTP %s %s" % (self.name, exc.code, detail)) from exc
        except (urllib.error.URLError, OSError) as exc:
            raise BackendUnavailable("无法连接 %s（%s）：%s"
                                     % (self.name, self.base_url, exc)) from exc
        try:
            return json.loads(raw)
        except ValueError as exc:
            raise BackendError("%s %s 返回的不是合法 JSON" % (self.name, path)) from exc

    # ── 契约实现 ────────────────────────────────────────────
    def is_available(self) -> bool:
        """能不能连上**并确认是 OpenAI 兼容服务**。

        ⚠️ 不只看"端口通不通" —— 必须 ``/v1/models`` 返回带 ``data`` 列表的
        JSON，否则可能把本机**其它** HTTP 服务误认成推理后端。
        """
        try:
            obj = self._http_get_json("/v1/models", timeout=min(5.0, self.timeout_sec))
        except BackendError:
            return False
        return isinstance(obj, dict) and isinstance(obj.get("data"), list)

    def list_models(self) -> List[str]:
        obj = self._http_get_json("/v1/models")
        names: List[str] = []
        for item in (obj.get("data") or []):
            if isinstance(item, dict):
                mid = item.get("id")
                if mid:
                    names.append(str(mid))
        return names

    async def generate(self, model: str, messages: Sequence[Dict[str, Any]],
                       temperature: float = 1.0, top_p: float = 1.0,
                       max_tokens: Optional[int] = None) -> AsyncIterator[GenerationChunk]:
        target = model or self.model
        if not target:
            raise BackendError("未指定模型名（配置 backend_model 或派单载荷里的 model）")
        body: Dict[str, Any] = {
            "model": target,
            "messages": [dict(m) for m in messages],
            "temperature": float(temperature),
            "top_p": float(top_p),
            "stream": True,
            # 请求后端在流里带上真实 usage（拿不到就退回估算，云端负责兜底）
            "stream_options": {"include_usage": True},
        }
        if max_tokens and int(max_tokens) > 0:
            body["max_tokens"] = int(max_tokens)

        import asyncio  # 局部导入：仅在真正生成时才需要事件循环

        loop = asyncio.get_event_loop()
        queue: "asyncio.Queue[Any]" = asyncio.Queue()
        _DONE = object()

        def _worker() -> None:
            """工作线程：逐行读 SSE，把分片塞进事件循环队列。"""
            saw_done = False
            saw_finish = False
            usage_payload: Optional[Dict[str, Any]] = None
            retried_without_stream_options = False
            try:
                while True:
                    req = urllib.request.Request(
                        self._url("/v1/chat/completions"),
                        data=json.dumps(body, ensure_ascii=False).encode("utf-8"),
                        method="POST",
                        headers=self._headers(),
                    )
                    try:
                        resp = urllib.request.urlopen(req, timeout=self.timeout_sec)
                    except urllib.error.HTTPError as exc:
                        detail = ""
                        try:
                            detail = exc.read().decode("utf-8", "replace")[:300]
                        except Exception:  # noqa: BLE001
                            detail = ""
                        # 兼容降级：老 llama.cpp 不认 stream_options ⇒ 去掉再试一次
                        if (exc.code in (400, 422)
                                and not retried_without_stream_options
                                and "stream_options" in detail):
                            retried_without_stream_options = True
                            body.pop("stream_options", None)
                            continue
                        raise BackendError("%s 返回 HTTP %s %s"
                                           % (self.name, exc.code, detail)) from exc
                    except (urllib.error.URLError, OSError) as exc:
                        raise BackendUnavailable("%s 连接失败（%s）：%s"
                                                 % (self.name, self.base_url, exc)) from exc

                    with resp:
                        for raw_line in resp:
                            line = raw_line.decode("utf-8", "replace").strip()
                            if not line or not line.startswith("data:"):
                                continue
                            data = line[5:].strip()
                            if data == "[DONE]":
                                saw_done = True
                                break
                            try:
                                obj = json.loads(data)
                            except ValueError:
                                continue  # 非 JSON 的 data 行直接跳过
                            if not isinstance(obj, dict):
                                continue
                            if obj.get("error"):
                                err = obj.get("error")
                                if isinstance(err, dict):
                                    err = err.get("message") or err
                                raise BackendError("%s 报错：%s" % (self.name, err))
                            # 真实 usage（可能出现在任意一帧，也可能是收尾的独立帧）
                            raw_usage = obj.get("usage")
                            if isinstance(raw_usage, dict):
                                usage_payload = raw_usage
                            choices = obj.get("choices") or []
                            if not choices:
                                continue
                            choice = choices[0] if isinstance(choices[0], dict) else {}
                            delta = choice.get("delta") or {}
                            content = delta.get("content") or ""
                            if choice.get("finish_reason"):
                                saw_finish = True
                            if content:
                                loop.call_soon_threadsafe(
                                    queue.put_nowait, GenerationChunk(text=content))
                    break

                # 收尾：把真实 usage 作为最后一个分片带出去
                if usage_payload:
                    try:
                        usage = BackendUsage(
                            prompt_tokens=int(usage_payload.get("prompt_tokens") or 0),
                            completion_tokens=int(usage_payload.get("completion_tokens") or 0),
                        )
                    except (TypeError, ValueError):
                        usage = None
                    if usage is not None:
                        loop.call_soon_threadsafe(
                            queue.put_nowait, GenerationChunk(text="", usage=usage, done=True))
                # ⚠️ 阶段六 P2 · A4 同构：**没收到 [DONE] 也没有 finish_reason** ⇒ 断流。
                #    少了这一句，半个答案会被当成完整答案计费。
                if not (saw_done or saw_finish):
                    raise BackendStreamBroken(
                        "%s 流中途断开（未收到 [DONE] 帧）" % self.name)
            except BackendError as exc:
                loop.call_soon_threadsafe(queue.put_nowait, exc)
            except (urllib.error.URLError, OSError) as exc:
                loop.call_soon_threadsafe(
                    queue.put_nowait, BackendUnavailable("%s 连接中断：%s" % (self.name, exc)))
            except Exception as exc:  # noqa: BLE001 - 兜底，绝不让线程静默死掉
                loop.call_soon_threadsafe(queue.put_nowait, BackendError(str(exc)))
            finally:
                loop.call_soon_threadsafe(queue.put_nowait, _DONE)

        threading.Thread(target=_worker, name="%s-gen" % self.name, daemon=True).start()

        while True:
            item = await queue.get()
            if item is _DONE:
                break
            if isinstance(item, Exception):
                raise item
            yield item


class VLLMBackend(OpenAICompatBackend):
    """vLLM 后端（高端多卡节点）。"""

    name = BACKEND_VLLM

    def __init__(self, base_url: str = DEFAULT_VLLM_BASE_URL,
                 timeout_sec: float = DEFAULT_TIMEOUT_SEC, model: str = "",
                 api_key: str = "") -> None:
        super().__init__(base_url or DEFAULT_VLLM_BASE_URL, timeout_sec, model, api_key)


class LlamaCppBackend(OpenAICompatBackend):
    """llama.cpp 后端（无 GPU 老机器）。"""

    name = BACKEND_LLAMACPP

    def __init__(self, base_url: str = DEFAULT_LLAMACPP_BASE_URL,
                 timeout_sec: float = DEFAULT_TIMEOUT_SEC, model: str = "",
                 api_key: str = "") -> None:
        super().__init__(base_url or DEFAULT_LLAMACPP_BASE_URL, timeout_sec, model, api_key)


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

    - ``auto``：构造**第一个候选**（Ollama）—— 真正的选点在
      :func:`get_backend`（那里才做探测）；
    - ``ollama``：显式 Ollama；
    - ``vllm`` / ``llamacpp``：OpenAI 兼容后端。

    返回 ``None`` 表示配置非法。
    """
    kind = str(_cfg_value(cfg, "inference_backend", BACKEND_AUTO) or BACKEND_AUTO).strip().lower()
    base_url = str(_cfg_value(cfg, "backend_base_url", "") or "").strip()
    model = str(_cfg_value(cfg, "backend_model", "") or "").strip()
    api_key = str(_cfg_value(cfg, "backend_api_key", "") or "").strip()
    try:
        timeout = float(_cfg_value(cfg, "backend_timeout_sec", DEFAULT_TIMEOUT_SEC)
                        or DEFAULT_TIMEOUT_SEC)
    except (TypeError, ValueError):
        timeout = DEFAULT_TIMEOUT_SEC

    if kind in (BACKEND_AUTO, BACKEND_OLLAMA):
        return OllamaBackend(base_url or DEFAULT_OLLAMA_BASE_URL, timeout, model)
    if kind == BACKEND_VLLM:
        return VLLMBackend(
            str(_cfg_value(cfg, "backend_vllm_base_url", "") or "").strip()
            or DEFAULT_VLLM_BASE_URL, timeout, model, api_key)
    if kind == BACKEND_LLAMACPP:
        return LlamaCppBackend(
            str(_cfg_value(cfg, "backend_llamacpp_base_url", "") or "").strip()
            or DEFAULT_LLAMACPP_BASE_URL, timeout, model, api_key)
    return None


def build_auto_candidates(cfg: Any = None) -> List[InferenceBackend]:
    """``auto`` 模式的候选后端列表（**按优先级**）。

    顺序：D1 拍板的「Ollama 优先」⇒ Ollama → vLLM → llama.cpp。

    ⚠️ **只有显式配置了地址的 OpenAI 兼容引擎才会进候选**：
    默认端口（8000 / 8080）在很多机器上已经被别的服务占着
    （比如 APIPaw 自己的网关就跑在 8000），盲目探测会把**不相干的服务**
    误认成推理后端 —— 那比"探不到"危险得多。
    想让 auto 顺带探 vLLM / llama.cpp，把对应地址写进配置即可。
    """
    base_url = str(_cfg_value(cfg, "backend_base_url", "") or "").strip()
    model = str(_cfg_value(cfg, "backend_model", "") or "").strip()
    api_key = str(_cfg_value(cfg, "backend_api_key", "") or "").strip()
    try:
        timeout = float(_cfg_value(cfg, "backend_timeout_sec", DEFAULT_TIMEOUT_SEC)
                        or DEFAULT_TIMEOUT_SEC)
    except (TypeError, ValueError):
        timeout = DEFAULT_TIMEOUT_SEC

    candidates: List[InferenceBackend] = [
        OllamaBackend(base_url or DEFAULT_OLLAMA_BASE_URL, timeout, model)]
    vllm_url = str(_cfg_value(cfg, "backend_vllm_base_url", "") or "").strip()
    if vllm_url:
        candidates.append(VLLMBackend(vllm_url, timeout, model, api_key))
    llamacpp_url = str(_cfg_value(cfg, "backend_llamacpp_base_url", "") or "").strip()
    if llamacpp_url:
        candidates.append(LlamaCppBackend(llamacpp_url, timeout, model, api_key))
    return candidates


def get_backend(cfg: Any = None, force_refresh: bool = False) -> Optional[InferenceBackend]:
    """获取当前生效的推理后端（带缓存）。

    - 配置为 ``auto`` 时会**逐个探测**候选（Ollama 优先）：全探不到 ⇒ 返回 ``None``
      （调用方据此回 ``backend_unavailable``，绝不回落模拟回答）。
    - 探测结果缓存 ``AUTO_PROBE_TTL_SEC`` 秒：推理引擎晚于节点启动时，
      节点能在 TTL 内自动发现它，无需重启。
    - 显式指定 ``vllm`` / ``llamacpp`` 时**不探测**（连不上就在真正调用时
      报 ``backend_unavailable``），避免每次心跳都白等超时。
    """
    global _backend_instance, _backend_key, _backend_probed_at

    kind = str(_cfg_value(cfg, "inference_backend", BACKEND_AUTO) or BACKEND_AUTO).strip().lower()
    base_url = str(_cfg_value(cfg, "backend_base_url", "") or "").strip()
    model = str(_cfg_value(cfg, "backend_model", "") or "").strip()
    vllm_url = str(_cfg_value(cfg, "backend_vllm_base_url", "") or "").strip()
    llamacpp_url = str(_cfg_value(cfg, "backend_llamacpp_base_url", "") or "").strip()
    try:
        timeout = float(_cfg_value(cfg, "backend_timeout_sec", DEFAULT_TIMEOUT_SEC)
                        or DEFAULT_TIMEOUT_SEC)
    except (TypeError, ValueError):
        timeout = DEFAULT_TIMEOUT_SEC
    key = "%s|%s|%s|%s|%s|%.1f" % (kind, base_url, model, vllm_url, llamacpp_url, timeout)

    now = time.time()
    with _backend_lock:
        if not force_refresh and _backend_instance is not None and _backend_key == key:
            if kind != BACKEND_AUTO or (now - _backend_probed_at) < AUTO_PROBE_TTL_SEC:
                return _backend_instance
        if kind == BACKEND_AUTO:
            picked: Optional[InferenceBackend] = None
            for candidate in build_auto_candidates(cfg):
                try:
                    if candidate.is_available():
                        picked = candidate
                        break
                except BackendError:
                    continue
        else:
            picked = build_backend(cfg)
        if picked is None:
            # auto 模式探不到 ⇒ 本节点**没有**推理引擎（返回 None，
            # 上层必须回 backend_unavailable）。
            _backend_instance = None
            _backend_key = key
            _backend_probed_at = now
            return None
        _backend_instance = picked
        _backend_key = key
        _backend_probed_at = now
        return picked


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
    "DEFAULT_VLLM_BASE_URL",
    "DEFAULT_LLAMACPP_BASE_URL",
    "BackendError",
    "BackendUnavailable",
    "BackendStreamBroken",
    "BackendUsage",
    "GenerationChunk",
    "InferenceBackend",
    "OllamaBackend",
    "OpenAICompatBackend",
    "VLLMBackend",
    "LlamaCppBackend",
    "build_backend",
    "build_auto_candidates",
    "get_backend",
    "reset_backend_cache",
    "describe_backend",
    "normalize_model_key",
    "resolve_model",
]
