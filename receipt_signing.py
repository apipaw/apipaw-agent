"""
receipt_signing.py — 推理签名收据（Proof-of-Inference · PoI，阶段十八 P0）

背景
----
云端要判定一个节点是否**真的**做了推理，必须有节点侧的**不可伪造**证据：
否则节点可以乱报 token 数、返回低熵垃圾、乃至伪造回执。

本模块提供**最小可验证**的收据：

    output_sha256 = sha256(output_text.encode("utf-8")).hexdigest()
    canonical     = "{request_id}:{model_id}:{prompt_tokens}:{completion_tokens}:{output_sha256}"
    receipt_sig   = HMAC-SHA256(key=node_secret, msg=canonical).hexdigest()

云端用**同一个 node_secret** 重算一遍即可验签：
- 签名对不上 ⇒ 收据被伪造；
- ``output_sha256`` 与云端自算的对不上 ⇒ 输出被调包；
- token 数与输出长度严重不符 ⇒ 疑似**虚报 token**（云端侧判定）。

⚠️ 口径**逐字节**约定（云端按同一份规范验签，勿改）：
  1. canonical 字段顺序固定、冒号分隔、**无空格**；
  2. 所有字段用 ``str()`` 归一（整数不带小数点，字符串原样）；
  3. 十六进制一律**小写**；
  4. 空输出照样签名（哈希空串）——云端把「空输出」当 ``SIMULATED``，
     **不是**当欺诈。

设计约束
--------
- **绝不抛异常**：任何畸形输入（空密钥 / 空文本 / 非字符串）都必须返回一个
  结构完好的字典，绝不让签名失败打断推理主流程；
- **只缓存最近一张收据**（模块级单槽），避免长跑进程内存无界增长。
"""

from __future__ import annotations

import hashlib
import hmac
import threading
from typing import Any, Dict, Optional

__all__ = [
    "sign_inference_receipt",
    "verify_inference_receipt",
    "remember_receipt",
    "last_receipt",
    "clear_receipt",
]


def _as_text(value: Any) -> str:
    """把任意值归一为字符串（``None`` ⇒ 空串，其余走 ``str()``）。"""
    if value is None:
        return ""
    if isinstance(value, str):
        return value
    try:
        return str(value)
    except Exception:  # noqa: BLE001 - 极端畸形对象的 str() 也会失败
        return ""


def _as_int(value: Any) -> int:
    """把任意值归一为整数（失败 ⇒ 0）。"""
    if isinstance(value, bool):
        return int(value)
    if isinstance(value, int):
        return value
    try:
        return int(float(value))
    except Exception:  # noqa: BLE001 - 非数值 ⇒ 0
        return 0


def _sha256_hex(text: str) -> str:
    """UTF-8 文本的 SHA-256 十六进制摘要（小写）。"""
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _canonical(
    request_id: str,
    model_id: str,
    prompt_tokens: int,
    completion_tokens: int,
    output_sha256: str,
) -> str:
    """构造规范化串（字段顺序与分隔符是**协议**，勿改）。"""
    return "%s:%s:%d:%d:%s" % (
        request_id,
        model_id,
        prompt_tokens,
        completion_tokens,
        output_sha256,
    )


def sign_inference_receipt(
    node_secret: str,
    request_id: str,
    model_id: str,
    prompt_tokens: int,
    completion_tokens: int,
    output_text: str,
) -> Dict[str, Any]:
    """
    为一次推理产出**签名收据**。

    返回::

        {
            "output_sha256": "<sha256 hex>",
            "receipt_sig": "<hmac-sha256 hex>",
            "canonical": "request_id:model_id:prompt:completion:sha256",
            "model_id": "<model id>",
            "output_chars": 12,
        }

    ⚠️ **绝不抛异常**：畸形输入一律降级为「空串 / 0」，仍返回结构完好的字典。
    """
    try:
        secret = _as_text(node_secret)
        rid = _as_text(request_id)
        mid = _as_text(model_id)
        p_tokens = _as_int(prompt_tokens)
        c_tokens = _as_int(completion_tokens)
        text = _as_text(output_text)

        output_sha256 = _sha256_hex(text)
        canonical = _canonical(rid, mid, p_tokens, c_tokens, output_sha256)
        sig = hmac.new(
            secret.encode("utf-8"),
            canonical.encode("utf-8"),
            hashlib.sha256,
        ).hexdigest()

        return {
            "output_sha256": output_sha256,
            "receipt_sig": sig,
            "canonical": canonical,
            # 模型 id 也随收据上送 —— 云端拿它做候选规范化串的兜底，
            # 避免「云端目录 id ↔ 本机 tag」命名不一致时误判诚实节点为欺诈。
            "model_id": mid,
            "output_chars": len(text),
        }
    except Exception:  # noqa: BLE001 - 签名失败绝不能打断推理主流程
        empty_sha = _sha256_hex("")
        return {
            "output_sha256": empty_sha,
            "receipt_sig": "",
            "canonical": "::0:0:%s" % empty_sha,
            "model_id": "",
            "output_chars": 0,
        }


def verify_inference_receipt(
    node_secret: str,
    canonical: str,
    receipt_sig: str,
) -> bool:
    """
    用节点密钥重算并比验签名（``hmac.compare_digest``，抗时序侧信道）。

    供**自检**与测试使用；云端按同一口径验签。
    """
    try:
        secret = _as_text(node_secret)
        canon = _as_text(canonical)
        sig = _as_text(receipt_sig)
        if not sig:
            return False
        expect = hmac.new(
            secret.encode("utf-8"),
            canon.encode("utf-8"),
            hashlib.sha256,
        ).hexdigest()
        return bool(hmac.compare_digest(expect, sig))
    except Exception:  # noqa: BLE001 - 验签异常 ⇒ 判否，绝不抛
        return False


# ──────────────────────────────────────────────────────────────
# 最近一张收据缓存（**单槽**，内存有界）
# ──────────────────────────────────────────────────────────────

_RECEIPT_LOCK = threading.Lock()
_LAST_RECEIPT: Optional[Dict[str, Any]] = None


def remember_receipt(receipt: Optional[Dict[str, Any]]) -> None:
    """记住**最近一张**收据（供 HTTP 心跳报文体携带）。"""
    global _LAST_RECEIPT
    if not receipt:
        return
    try:
        snapshot = {
            "output_sha256": _as_text(receipt.get("output_sha256")),
            "receipt_sig": _as_text(receipt.get("receipt_sig")),
            "canonical": _as_text(receipt.get("canonical")),
            "model_id": _as_text(receipt.get("model_id")),
            "output_chars": _as_int(receipt.get("output_chars")),
        }
    except Exception:  # noqa: BLE001 - 畸形收据直接忽略
        return
    with _RECEIPT_LOCK:
        _LAST_RECEIPT = snapshot


def last_receipt() -> Optional[Dict[str, Any]]:
    """取出最近一张收据（副本；没有则 ``None``）。"""
    with _RECEIPT_LOCK:
        snapshot = _LAST_RECEIPT
    if snapshot is None:
        return None
    return dict(snapshot)


def clear_receipt() -> None:
    """清空缓存（测试用）。"""
    global _LAST_RECEIPT
    with _RECEIPT_LOCK:
        _LAST_RECEIPT = None
