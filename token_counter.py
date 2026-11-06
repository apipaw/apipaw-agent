"""
token_counter.py — APIPaw Agent 精确 token 计数

云端计费需要**真实**的 token 消耗，而不是"按字符数除以 4"的粗估。本模块按优先级
选择分词后端：

1. **本地 HF 分词器**（可选依赖 ``transformers`` + 配置了 ``tokenizer_path``）：
   节点本地跑 Qwen / Llama 时用它，计数与本地推理运行时完全一致；
2. **``tiktoken``**（可选依赖）：BPE 精确分词，装了就用；
3. **启发式回退**：CJK 感知估算（中日韩字符 ≈ 1 token，其余 ≈ 4 字符 ≈ 1 token）。

设计约束：
- 后端探测只做一次并缓存（进程内加锁），探测失败绝不影响主流程；
- ``transformers`` / ``tiktoken`` 都是**可选**依赖，不强制安装，也不写进 requirements；
- 所有对外函数都保证不抛异常（分词失败自动退回启发式）。

分词器路径来源（优先级从高到低）：
``tokenizer_path`` 配置项 > 环境变量 ``APIPAW_TOKENIZER_PATH``
"""

from __future__ import annotations

import os
import re
import threading
from typing import Any, Dict, List, Optional, Tuple

# CJK 表意文字 / 假名 / 谚文 / 全角符号：这类字符通常 1 字 ≈ 1 token
_CJK_RE = re.compile(
    "[぀-ヿ㐀-䶿一-鿿豈-﫿"
    "ｦ-ﾟ가-힯]"
)

# 每条消息的协议开销（role / 分隔符），与主流 chat 模板的经验值一致
_PER_MESSAGE_OVERHEAD = 3
_REPLY_PRIMING_OVERHEAD = 3

_lock = threading.Lock()
_backend: Optional[Tuple[str, Any]] = None
_probed = False


# ──────────────────────────────────────────────────────────────
# 后端探测
# ──────────────────────────────────────────────────────────────
def _try_hf_tokenizer(path: str) -> Optional[Tuple[str, Any]]:
    """加载本地 HF 分词器；缺依赖或模型文件不存在时返回 None。"""
    if not path:
        return None
    try:
        from transformers import AutoTokenizer  # type: ignore[import-not-found]
    except Exception:
        return None
    try:
        tokenizer = AutoTokenizer.from_pretrained(path)
    except Exception:
        return None
    return ("hf:%s" % path, tokenizer)


def _try_tiktoken() -> Optional[Tuple[str, Any]]:
    """加载 tiktoken 的 cl100k_base 编码器。"""
    try:
        import tiktoken  # type: ignore[import-not-found]
    except Exception:
        return None
    try:
        encoding = tiktoken.get_encoding("cl100k_base")
    except Exception:
        return None
    return ("tiktoken", encoding)


def _tokenizer_path() -> str:
    """读取配置里的分词器路径（配置项优先于环境变量）。"""
    path = ""
    try:
        from agent_config import get_config

        path = str(getattr(get_config(), "tokenizer_path", "") or "")
    except Exception:
        path = ""
    return path.strip() or os.getenv("APIPAW_TOKENIZER_PATH", "").strip()


def _detect_backend() -> Optional[Tuple[str, Any]]:
    """探测并缓存分词后端（进程内只做一次）。"""
    global _backend, _probed
    with _lock:
        if _probed:
            return _backend
        _probed = True
        backend = _try_hf_tokenizer(_tokenizer_path())
        if backend is None:
            backend = _try_tiktoken()
        _backend = backend
        return _backend


def backend_name() -> str:
    """当前生效的计数后端名（用于日志与排障）。"""
    backend = _detect_backend()
    return backend[0] if backend else "heuristic"


# ──────────────────────────────────────────────────────────────
# 计数
# ──────────────────────────────────────────────────────────────
def _iter_runs(text: str):
    """把文本切成 (片段, 是否 CJK) 的连续段。"""
    start = 0
    current = bool(_CJK_RE.match(text[0])) if text else False
    for index in range(1, len(text)):
        is_cjk = bool(_CJK_RE.match(text[index]))
        if is_cjk != current:
            yield text[start:index], current
            start = index
            current = is_cjk
    if text:
        yield text[start:], current


def _heuristic_tokens(text: str) -> int:
    """CJK 感知的启发式计数（无分词器时的回退）。"""
    if not text:
        return 0
    total = 0
    for run, is_cjk in _iter_runs(text):
        if is_cjk:
            total += len(run)
        elif run.strip():
            # 英文/数字/符号：约 4 字符 1 token
            total += max(1, (len(run) + 3) // 4)
    return total


def count_tokens(text: str) -> int:
    """统计一段文本的 token 数（永不抛异常）。"""
    if not text:
        return 0
    backend = _detect_backend()
    if backend is not None:
        kind, tokenizer = backend
        try:
            if kind == "tiktoken":
                return len(tokenizer.encode(text, disallowed_special=()))
            return len(tokenizer.encode(text, add_special_tokens=False))
        except Exception:
            pass
    return _heuristic_tokens(text)


def count_prompt_tokens(messages: List[Dict[str, Any]]) -> int:
    """统计一段 chat 消息的 prompt token（含每条消息的协议开销）。"""
    total = _REPLY_PRIMING_OVERHEAD
    for message in messages or []:
        content = message.get("content") or ""
        total += count_tokens(content) + _PER_MESSAGE_OVERHEAD
    return total


def build_usage(prompt_tokens: int, completion_tokens: int) -> Dict[str, int]:
    """构造 OpenAI 规范的 usage 结构。"""
    prompt_tokens = max(0, int(prompt_tokens or 0))
    completion_tokens = max(0, int(completion_tokens or 0))
    return {
        "prompt_tokens": prompt_tokens,
        "completion_tokens": completion_tokens,
        "total_tokens": prompt_tokens + completion_tokens,
    }
