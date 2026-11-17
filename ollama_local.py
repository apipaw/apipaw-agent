"""
ollama_local.py —— 阶段十三 P2 · 本机 Ollama 模型管理（免命令行一键拉取）

背景
----
节点运营者要在本机装一个模型接单，过去只有两条路：命令行敲
``ollama pull qwen2.5:0.5b``，或者手工改 ``agent.config.json``。
阶段十三 P2 把它变成**网页点两下**：

1. ``GET  /api/ollama/tags`` —— 探测本机已安装模型 + 在线状态；
2. ``POST /api/ollama/pull`` —— 校验模型名后**触发拉取**，拉完自动把
   ``backend_model`` / ``model_name`` 写进 ``agent.config.json``。

设计约束
--------
- **只跟本机 127.0.0.1 的 Ollama 说话**，绝不外呼（云端推荐列表由浏览器
  直接读云端公开端点，不经过本机）；
- **拉取放后台线程**：本机 HTTP 面板不能被一个几百 MB 的下载阻塞住
  （浏览器会超时，Agent 的 ``/health`` 也会被拖死）；
- **模型名严格白名单校验**：模型名会被拼进本机 URL，必须防路径穿越 /
  参数注入 / 误拉巨型模型。

⚠️ 本模块**不碰** ``agent.config.json`` —— 落盘由调用方（``main.py`` 的
端点层）用传入的配置对象完成 ⇒ 单元测试可以注入临时配置，
**绝不污染用户真实配置**（阶段八 P0 的血泪教训）。
"""

from __future__ import annotations

import json
import re
import threading
import time
import urllib.error
import urllib.request
from typing import Any, Callable, Dict, List, Optional, Tuple

#: 本机 Ollama 默认地址（与 ``agent_config.backend_base_url`` 默认值一致）
DEFAULT_OLLAMA_BASE_URL = "http://127.0.0.1:11434"

#: 探测本机的超时（秒）—— 面板要"秒回"，不能干等
PROBE_TIMEOUT_SEC = 5.0

#: 拉取是后台线程 ⇒ 单次拉取的看门狗超时（秒）
PULL_TIMEOUT_SEC = 3600.0

#: 模型名白名单：``名字`` 或 ``名字:标签``（可带 ``/`` 命名空间，如 ``library/llama3``）
#: ⚠️ 允许字母数字与 ``._-/`` —— 其它字符（空格、引号、``..``）一律拒绝。
_MODEL_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._\-/]{0,127}$")
_TAG_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._\-]{0,63}$")


class OllamaError(Exception):
    """本机 Ollama 调用失败（连不上 / 报错 / 返回体非法）。"""


def validate_model_name(name: Any) -> str:
    """校验模型名合法性，返回**规范化**后的名字；不合法抛 :class:`ValueError`。

    规则（阶段十三 P2）：

    - 去掉首尾空白；
    - 允许 ``repo/name`` 形态（Ollama 命名空间），但**禁止**以 ``/`` 开头
      或以 ``/`` 结尾、**禁止**出现 ``..``（路径穿越）；
    - ``:标签`` 段必须合法（字母数字与 ``._-``）；
    - 名字本身不允许空格 / 引号 / 反斜杠等一切"能被 URL 解释"的字符。
    """
    text = str(name or "").strip()
    if not text:
        raise ValueError("模型名不能为空")
    if len(text) > 160:
        raise ValueError("模型名过长（上限 160 字符）")
    if ".." in text:
        raise ValueError("模型名非法：不允许出现 '..'")
    if text.startswith("/") or text.endswith("/"):
        raise ValueError("模型名非法：不能以 '/' 开头或结尾")
    if "\\" in text or " " in text or '"' in text or "'" in text:
        raise ValueError("模型名非法：只允许字母数字与 . _ - / :")
    base, sep, tag = text.partition(":")
    if not _MODEL_NAME_RE.match(base):
        raise ValueError("模型名非法：只允许字母数字与 . _ - /")
    if sep:
        if not tag:
            raise ValueError("模型名非法：':' 后必须有标签")
        if not _TAG_RE.match(tag):
            raise ValueError("模型名非法：标签只允许字母数字与 . _ -")
    return text


def _http_json(url: str, payload: Optional[Dict[str, Any]] = None,
               timeout: float = PROBE_TIMEOUT_SEC,
               method: str = "GET") -> Any:
    """本机 HTTP JSON 调用（**只允许本机地址**）。"""
    data = None
    if payload is not None:
        data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    req = urllib.request.Request(
        url, data=data, method=method,
        headers={"Content-Type": "application/json", "Accept": "application/json"},
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw = resp.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as exc:
        detail = ""
        try:
            detail = exc.read().decode("utf-8", "replace")[:200]
        except Exception:  # noqa: BLE001 - 读错误体失败不影响结论
            detail = ""
        raise OllamaError("Ollama 返回 HTTP %s %s" % (exc.code, detail)) from exc
    except (urllib.error.URLError, OSError) as exc:
        raise OllamaError("无法连接本机 Ollama：%s" % exc) from exc
    try:
        return json.loads(raw)
    except ValueError as exc:
        raise OllamaError("Ollama 返回的不是合法 JSON") from exc


def list_local_models(base_url: str = DEFAULT_OLLAMA_BASE_URL,
                      timeout: float = PROBE_TIMEOUT_SEC) -> Dict[str, Any]:
    """探测本机 Ollama 已安装的模型。

    返回 ``{"ok": bool, "online": bool, "models": [...], "error": str}``。
    ⚠️ **Ollama 没起来不算错误** —— 返回 ``online=False`` + 原因，
    让面板能显示"本机没装 Ollama"而不是一片红。
    """
    url = (base_url or DEFAULT_OLLAMA_BASE_URL).rstrip("/") + "/api/tags"
    try:
        obj = _http_json(url, None, timeout)
    except OllamaError as exc:
        return {"ok": True, "online": False, "models": [], "error": str(exc)}
    models: List[Dict[str, Any]] = []
    for item in (obj.get("models") or []) if isinstance(obj, dict) else []:
        if not isinstance(item, dict):
            continue
        name = item.get("name") or item.get("model")
        if not name:
            continue
        details = item.get("details") or {}
        models.append({
            "name": str(name),
            "size": int(item.get("size") or 0),
            "family": str(details.get("family") or ""),
            "parameter_size": str(details.get("parameter_size") or ""),
        })
    models.sort(key=lambda m: m["name"])
    return {"ok": True, "online": True, "models": models, "error": ""}


class PullManager:
    """**后台线程**拉取管理器（同一时刻只允许一个拉取任务）。

    状态机：``idle`` → ``running`` → ``done`` / ``error``。
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._thread: Optional[threading.Thread] = None
        self._state: Dict[str, Any] = {
            "model": "", "state": "idle", "status": "", "error": "",
            "completed": 0, "total": 0, "started_at": 0.0, "finished_at": 0.0,
        }

    # ── 状态读取 ──────────────────────────────────────────
    def status(self) -> Dict[str, Any]:
        with self._lock:
            return dict(self._state)

    def is_running(self) -> bool:
        with self._lock:
            return self._state.get("state") == "running"

    # ── 触发拉取 ──────────────────────────────────────────
    def start(self, model: str, base_url: str = DEFAULT_OLLAMA_BASE_URL,
              on_success: Optional[Callable[[str], None]] = None,
              timeout: float = PULL_TIMEOUT_SEC) -> Dict[str, Any]:
        """启动一次后台拉取。返回 ``{"ok","started","reason","model"}``。

        - 已有拉取在跑 ⇒ ``{"ok": False, "reason": "busy"}``；
        - ``on_success(model)`` 在拉取**成功**后调用（落盘由调用方做），
          回调异常**不影响**拉取状态。
        """
        name = validate_model_name(model)
        if self.is_running():
            return {"ok": False, "started": False, "reason": "busy", "model": name}
        with self._lock:
            self._state = {
                "model": name, "state": "running", "status": "starting",
                "error": "", "completed": 0, "total": 0,
                "started_at": time.time(), "finished_at": 0.0,
            }
        thread = threading.Thread(
            target=self._run, args=(name, base_url, on_success, timeout),
            name="apipaw-ollama-pull", daemon=True,
        )
        self._thread = thread
        thread.start()
        return {"ok": True, "started": True, "reason": "", "model": name}

    # ── 拉取线程体 ────────────────────────────────────────
    def _run(self, model: str, base_url: str,
             on_success: Optional[Callable[[str], None]],
             timeout: float) -> None:
        url = (base_url or DEFAULT_OLLAMA_BASE_URL).rstrip("/") + "/api/pull"
        error = ""
        status = ""
        completed = 0
        total = 0
        try:
            req = urllib.request.Request(
                url, data=json.dumps({"model": model, "stream": True}).encode("utf-8"),
                method="POST",
                headers={"Content-Type": "application/json"},
            )
            deadline = time.time() + float(timeout)
            with urllib.request.urlopen(req, timeout=60) as resp:
                for raw in resp:
                    line = raw.decode("utf-8", "replace").strip()
                    if not line:
                        continue
                    if time.time() > deadline:
                        raise OllamaError("拉取超时（超过 %.0f 秒）" % timeout)
                    try:
                        obj = json.loads(line)
                    except ValueError:
                        continue
                    if not isinstance(obj, dict):
                        continue
                    if obj.get("error"):
                        raise OllamaError(str(obj.get("error")))
                    status = str(obj.get("status") or status)
                    try:
                        completed = int(obj.get("completed") or completed)
                        total = int(obj.get("total") or total)
                    except (TypeError, ValueError):
                        pass
                    self._update(status=status, completed=completed, total=total)
        except OllamaError as exc:
            error = str(exc)
        except Exception as exc:  # noqa: BLE001 - 后台线程绝不能把异常带出去
            error = "拉取失败：%s" % exc
        if error:
            self._finish(state="error", status=status, error=error,
                         completed=completed, total=total)
            return
        # ⚠️ 顺序很重要：**先落盘再标记 done**。
        # 此前是先 ``_finish(state="done")`` 再写配置 —— 于是「拉取已完成」
        # 的状态会**早于**配置写盘落地，调用方（如面板轮询 / 阶段十三用例）
        # 读到 done 时读到的还是旧配置 ⇒ 偶发「拉取成功但模型没绑上」。
        if on_success is not None:
            try:
                on_success(model)
            except Exception as exc:  # noqa: BLE001 - 落盘失败不影响拉取结论
                print("[ollama] 拉取成功但写入配置失败: %s" % exc)
        self._finish(state="done", status=status or "success", error="",
                     completed=completed, total=total)

    def _update(self, status: str = "", completed: int = 0, total: int = 0) -> None:
        with self._lock:
            self._state["status"] = status or self._state.get("status", "")
            self._state["completed"] = int(completed or 0)
            self._state["total"] = int(total or 0)

    def _finish(self, state: str, status: str, error: str,
                completed: int, total: int) -> None:
        with self._lock:
            self._state["state"] = state
            self._state["status"] = status
            self._state["error"] = error
            self._state["completed"] = int(completed or 0)
            self._state["total"] = int(total or 0)
            self._state["finished_at"] = time.time()


# ---------------------------------------------------------------------------
# 阶段十五 P2 · 云端黄金记忆 ⇒ 本机 Ollama **合成进化模型**
# ---------------------------------------------------------------------------
#
# 云端公开端点 ``/api/v1/network/golden-modelfile`` 会回一段**标准 Ollama
# Modelfile**（``FROM`` / ``PARAMETER`` / ``SYSTEM`` / ``MESSAGE``）。
# 本机只要把它交给 Ollama 的 ``POST /api/create``，就能在本地得到一个
# "带着云端黄金 Few-Shot 的派生模型" —— **零训练成本、零显卡**。
#
# 为什么自己解析 Modelfile 而不是直接把文本丢给 Ollama：
# - 老版本 Ollama 的 ``/api/create`` **不认** ``modelfile`` 字段（只认结构化的
#   ``from`` / ``system`` / ``messages`` / ``parameters``）；
# - 自己解析还能在**调用前**就知道云端到底给了多少范例（可回显给用户）。

#: 合成模型是**本地**操作（复制 base 层 + 写入 system/messages），
#: 但冷缓存 / 大 base 模型可能要几分钟 ⇒ 看门狗给足。
CREATE_TIMEOUT_SEC = 900.0


def derive_evolved_model_name(base_model: str) -> str:
    """由基准模型推导**进化模型名**。

    ``qwen2.5:0.5b`` → ``qwen2.5-apipaw-golden:latest``
    ``library/llama3:8b`` → ``llama3-apipaw-golden:latest``

    规则：取 ``:`` 前的**模型名**（去掉 ``library/`` 命名空间），拼上
    ``-apipaw-golden:latest``。名字本身会被 :func:`validate_model_name` 复检。
    """
    text = str(base_model or "").strip()
    if not text:
        return "qwen2.5-apipaw-golden:latest"
    base = text.split(":", 1)[0].strip()
    if base.startswith("library/"):
        base = base[len("library/"):]
    base = base.strip() or "qwen2.5"
    # 已经是个进化模型了 ⇒ **不要再叠一层**后缀
    # （`qwen2.5-apipaw-golden:latest` 推一次不该变成 `…-apipaw-golden-apipaw-golden`）
    if base.endswith("-apipaw-golden"):
        return "%s:latest" % base
    return "%s-apipaw-golden:latest" % base


def _strip_quotes(value: str) -> str:
    """去掉 Modelfile 里的引号包裹（``\"\"\"...\"\"\"`` / ``\"...\"`` / ``'...'``）。"""
    text = str(value or "").strip()
    if len(text) >= 6 and text.startswith('"""') and text.endswith('"""'):
        return text[3:-3]
    if len(text) >= 2 and text.startswith('"') and text.endswith('"'):
        return text[1:-1]
    if len(text) >= 2 and text.startswith("'") and text.endswith("'"):
        return text[1:-1]
    return text


def _coerce_param(value: str) -> Any:
    """PARAMETER 的值：能转成数字就转（Ollama 的 parameters 是结构化字段）。"""
    text = str(value or "").strip()
    try:
        if re.fullmatch(r"-?\d+", text):
            return int(text)
        if re.fullmatch(r"-?\d*\.\d+", text):
            return float(text)
    except Exception:  # noqa: BLE001 - 正则异常不该影响合成
        pass
    return text


def _read_quoted_block(lines: List[str], idx: int, first: str) -> Tuple[str, int]:
    """读取一个（可能跨行的）三引号块。

    返回 ``(内容, 下一行下标)``。

    ⚠️ **阶段十六 P1 修的真 bug**：Ollama Modelfile 的 ``SYSTEM`` / ``MESSAGE``
    正文可以跨行（``MESSAGE assistant \"\"\"def top_n(items, n):`` 换行后再
    ``from collections import Counter`` 收尾）。老实现按行平铺解析 ⇒

    1. 正文里的 ``from collections import Counter`` 被当成 ``FROM`` 指令
       ⇒ ``from`` 被改成 ``"collections import Counter"`` ⇒ 本机 Ollama 直接回
       ``invalid model name``，**进化模型永远合成不出来**；
    2. 跨行的 SYSTEM 正文被截断成第一行。

    这里按"三引号开闭"把整块正文**一次性收走**，块内的行**不再参与指令解析**。
    """
    text = str(first or "")
    if not (text.startswith('"""') or text.startswith("'''")):
        return _strip_quotes(text), idx
    quote = text[:3]
    rest = text[3:]
    end = rest.find(quote)
    if end >= 0:
        # 单行闭合
        return rest[:end].strip(), idx
    parts = [rest]
    total = len(lines)
    while idx < total:
        line = lines[idx]
        idx += 1
        pos = line.find(quote)
        if pos >= 0:
            parts.append(line[:pos])
            break
        parts.append(line)
    return "\n".join(parts).strip(), idx


def parse_modelfile(text: str) -> Dict[str, Any]:
    """把标准 Ollama Modelfile 解析成 ``/api/create`` 要的结构。

    返回::

        {"from": str, "system": str, "messages": [{"role","content"}],
         "parameters": {...}}

    ⚠️ **只解析白名单指令**（``FROM`` / ``PARAMETER`` / ``SYSTEM`` / ``MESSAGE``）
    —— 云端 Modelfile 里若混进别的指令（比如 ``RUN``），一律忽略
    （本机 Agent 不应该被远端文本诱导去执行任何东西）。

    ⚠️ 指令必须**顶格**（列 0）才生效 ⇒ 块正文里的缩进行永远不会被误认成指令。
    """
    out: Dict[str, Any] = {
        "from": "", "system": "", "messages": [], "parameters": {},
    }
    lines = str(text or "").splitlines()
    idx = 0
    total = len(lines)
    while idx < total:
        raw = lines[idx]
        idx += 1
        # 指令必须顶格；缩进行只能是块正文
        if not raw or not raw[:1].isalpha():
            continue
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        upper = line.upper()
        if upper.startswith("FROM "):
            out["from"] = line[len("FROM "):].strip()
        elif upper.startswith("PARAMETER "):
            body = line[len("PARAMETER "):].strip()
            if not body:
                continue
            parts = body.split(None, 1)
            if len(parts) == 2:
                out["parameters"][parts[0]] = _coerce_param(parts[1])
        elif upper.startswith("SYSTEM "):
            value, idx = _read_quoted_block(lines, idx, line[len("SYSTEM "):])
            out["system"] = value
        elif upper.startswith("MESSAGE "):
            body = line[len("MESSAGE "):].strip()
            role, _, content = body.partition(" ")
            role = role.strip().lower()
            if role not in ("user", "assistant", "system"):
                continue
            value, idx = _read_quoted_block(lines, idx, content)
            out["messages"].append({"role": role, "content": value})
    return out


def create_model_from_modelfile(base_url: str, name: str,
                                 modelfile_text: str,
                                 timeout: float = CREATE_TIMEOUT_SEC
                                 ) -> Dict[str, Any]:
    """用云端 Modelfile 在**本机** Ollama 上合成一个派生模型。

    返回 ``{"ok", "error", "name", "from", "messages", "system"}``。

    - 本机 Ollama **没起来** ⇒ ``{"ok": False, "error": "本机 Ollama 未运行…"}``
      （调用方据此给用户一句人话，而不是 500）；
    - Modelfile 解析不出 ``FROM`` ⇒ 拒绝合成（不能凭空造一个模型）。
    """
    parsed = parse_modelfile(modelfile_text)
    if not parsed.get("from"):
        return {"ok": False, "error": "云端 Modelfile 缺少 FROM 行，无法合成",
                "name": name, "from": "", "messages": 0, "system": ""}
    payload: Dict[str, Any] = {"model": name, "stream": False}
    payload["from"] = parsed["from"]
    if parsed.get("system"):
        payload["system"] = parsed["system"]
    if parsed.get("messages"):
        payload["messages"] = parsed["messages"]
    if parsed.get("parameters"):
        payload["parameters"] = parsed["parameters"]
    url = (base_url or DEFAULT_OLLAMA_BASE_URL).rstrip("/") + "/api/create"
    try:
        obj = _http_json(url, payload, timeout, method="POST")
    except OllamaError as exc:
        return {"ok": False, "error": str(exc), "name": name,
                "from": parsed["from"], "messages": len(parsed["messages"]),
                "system": len(parsed["system"] or "")}
    status = ""
    if isinstance(obj, dict):
        status = str(obj.get("status") or obj.get("error") or "")
    if status and status.lower() not in ("success", "ok", ""):
        return {"ok": False, "error": "Ollama 返回：%s" % status, "name": name,
                "from": parsed["from"], "messages": len(parsed["messages"]),
                "system": len(parsed["system"] or "")}
    return {"ok": True, "error": "", "name": name, "from": parsed["from"],
            "messages": len(parsed["messages"]),
            "system": len(parsed["system"] or "")}


__all__ = [
    "DEFAULT_OLLAMA_BASE_URL",
    "OllamaError",
    "PullManager",
    "create_model_from_modelfile",
    "derive_evolved_model_name",
    "list_local_models",
    "parse_modelfile",
    "validate_model_name",
]
