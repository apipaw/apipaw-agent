# -*- coding: utf-8 -*-
"""阶段五 P4 · 模块 B：派单协议 v2 的**模型指令加密**（零第三方依赖）。

为什么需要它（蓝图 §3.1 · §3.2）
--------------------------------
盲派单的目标是：**节点在挑单之前看不到任何商业信息** ——
不知道这一单是哪个模型、单价多少、场景 A 还是 B。

过渡期（v1 载荷）里云端同时下发明文 ``model`` 与不透明 ``model_ref``，
节点看得到模型名 ⇒ 知道模型规模 ⇒ 能反推场景与量级 ⇒ 仍然具备"挑单"的信息基础。

v2 的做法：**云端只发加密后的模型指令**
- 帧里出现的是 ``model_ref``（加盐散列，字典穷举还原不出）与 ``model_cipher``；
- ``model_cipher`` 用**该节点自己的密钥**派生出的对称密钥加密；
- 节点用自己的密钥解开 —— 它必须知道模型名才能加载权重，这是物理上无法回避的；
- 但它**拿不到目录**（一次只解出一个模型）、**拿不到价格/费率/角色**。

⚠️ 诚实边界：节点**持有自己的密钥**，所以它理论上能解开当前这一单、
甚至（若拿到别的节点的密钥）解开别的单。这不是密码学意义上的"绝对不可知"，
而是"**不主动提供可枚举的信息**"——
真正的防线是**不让节点按订单挑拣**（公平排序 + 让退冷却 + 回执瘦身），
加密只是把"顺手就能看到的价格表"这道口子堵上。

⚠️ 兼容性：本模块是**纯增量**。
- v1 节点（不声明 protocol_version）收到的还是老的明文载荷；
- v2 节点收到的载荷里**没有** ``model`` 字段，只有 ``model_cipher``；
- 节点侧解不开时（密钥不匹配/版本旧）应回退报错，云端据此重试或回退模拟回答。

算法（标准库即可，Python 3.6+ / Windows / 源站 3.6.8 全一致）
-----------------------------------------------------------
- 密钥派生：``key = SHA256("apipaw-dispatch-v2|" + node_secret)``
- 密钥流：``HMAC-SHA256(key, nonce || counter)`` 逐块取，与明文 XOR
- 密文格式：``v2:<nonce_hex>:<cipher_hex>``（nonce = 16 字节随机）
- 完整性：密文里带 SHA256 摘要前 8 字节做校验，防止篡改后解出乱码

云端与 Agent 各存一份**同名同实现**的模块（两个仓库独立分发），
由 ``test_dispatch_protocol_v2.py`` 做**跨仓一致性**回归。
"""
import hashlib
import hmac
import os
from typing import Optional

PROTOCOL_VERSION = 2
CIPHER_PREFIX = "v2"
_KEY_LABEL = "apipaw-dispatch-v2|"


def derive_key(node_secret: str) -> bytes:
    """从节点密钥派生对称密钥（32 字节）。"""
    if not node_secret:
        raise ValueError("node_secret 不能为空")
    return hashlib.sha256((_KEY_LABEL + str(node_secret)).encode("utf-8")).digest()


def _keystream(key: bytes, nonce: bytes, length: int) -> bytes:
    """HMAC-SHA256 计数器模式密钥流。"""
    out = bytearray()
    counter = 0
    while len(out) < length:
        out.extend(hmac.new(key, nonce + counter.to_bytes(4, "big"),
                            hashlib.sha256).digest())
        counter += 1
    return bytes(out[:length])


def encrypt_model(model_id: str, node_secret: str, nonce: Optional[bytes] = None) -> str:
    """把模型 id 加密成可下发字符串。

    返回格式：``v2:<nonce_hex>:<cipher_hex>``。
    """
    if not model_id:
        raise ValueError("model_id 不能为空")
    key = derive_key(node_secret)
    if nonce is None:
        nonce = os.urandom(16)
    plain = str(model_id).encode("utf-8")
    cipher = bytes(a ^ b for a, b in zip(plain, _keystream(key, nonce, len(plain))))
    digest = hashlib.sha256(plain).digest()[:8]
    return "%s:%s:%s" % (CIPHER_PREFIX, nonce.hex(),
                         (digest + cipher).hex())


def decrypt_model(cipher_text: str, node_secret: str) -> str:
    """解开模型指令；失败抛 ``ValueError``（调用方必须回退，不能静默吞）。"""
    if not cipher_text:
        raise ValueError("密文为空")
    parts = str(cipher_text).split(":")
    if len(parts) != 3 or parts[0] != CIPHER_PREFIX:
        raise ValueError("密文格式非法")
    try:
        nonce = bytes.fromhex(parts[1])
        blob = bytes.fromhex(parts[2])
    except ValueError:
        raise ValueError("密文十六进制非法")
    if len(blob) < 8:
        raise ValueError("密文长度不足")
    digest, cipher = blob[:8], blob[8:]
    key = derive_key(node_secret)
    plain = bytes(a ^ b for a, b in zip(cipher, _keystream(key, nonce, len(cipher))))
    if not hmac.compare_digest(digest, hashlib.sha256(plain).digest()[:8]):
        raise ValueError("密文校验失败（密钥不匹配或内容被篡改）")
    return plain.decode("utf-8")
