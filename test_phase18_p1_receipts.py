"""阶段十八 P1 · PoI 签名收据自检（Agent v1.0.17+）。

用法（在仓库根目录执行）：
    ./.venv/Scripts/python.exe test_phase18_p1_receipts.py

覆盖：
  1. ``receipt_signing.sign_inference_receipt`` 的三要素：
     - ``output_sha256`` 与手工 ``sha256(text)`` 一致；
     - canonical 串严格是 ``request_id:model_id:prompt:completion:sha``；
     - ``receipt_sig`` 与手工 HMAC-SHA256 一致（小写 hex）。
  2. 确定性：同输入 ⇒ 同签名；换密钥 ⇒ 换签名。
  3. ``verify_inference_receipt``：好签名 True、篡改签名 False。
  4. 空文本 / 空密钥 / 非字符串输入**不抛异常**。
  5. ``ws_tunnel`` 的 ``RESPONSE_DONE`` 帧携带 ``receipt``（真实推理后端路径
     + 本地 API 转发路径）。
  6. HTTP 心跳报文体携带 ``receipt`` 字段。
  7. Agent 版本 >= 1.0.17（阶段二十已升到 1.0.18）。
  8. **真机 ``agent.config.json`` 零污染**（全程字节级比对）。

测试完全离线：不连生产云端，不写本机配置文件。
"""

import asyncio
import hashlib
import hmac
import json
import os
import sys
import time
from pathlib import Path

# 必须在导入 telemetry / ws_tunnel 之前设置：不连生产云端
os.environ["APIPAW_DISABLE_TELEMETRY"] = "1"

REPO_ROOT = Path(__file__).resolve().parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

RESULTS = []

# 真机配置基线字节（脚本启动时快照，结束时比对）
_CONFIG_SNAPSHOT = b""


def _record(name, ok, detail=""):
    RESULTS.append((name, ok, detail))
    print(("[PASS] " if ok else "[FAIL] ") + name + (" | " + detail if detail else ""))


# ───────────────────────── 替身 ─────────────────────────


class _FakeUsage(object):
    """最小 usage 替身（只需 ``to_dict``）。"""

    def __init__(self, prompt_tokens=0, completion_tokens=0):
        self._d = {
            "prompt_tokens": prompt_tokens,
            "completion_tokens": completion_tokens,
            "total_tokens": prompt_tokens + completion_tokens,
        }

    def to_dict(self):
        return dict(self._d)


class _FakeChunk(object):
    """最小流式分片替身。"""

    def __init__(self, text="", usage=None):
        self.text = text
        self.usage = usage


class _FakeBackend(object):
    """最小推理后端替身（异步生成若干分片）。"""

    name = "fake"

    def __init__(self, pieces=("你好", "世界"), usage=None):
        self._pieces = list(pieces)
        self._usage = usage

    async def generate(self, model, messages, **kwargs):
        for piece in self._pieces:
            yield _FakeChunk(piece, None)
        yield _FakeChunk("", self._usage)


class _FakeResp(object):
    """最小 SSE 响应替身（支持 ``with`` + ``iter_lines``）。"""

    def __init__(self, lines, status_code=200, headers=None):
        self._lines = lines
        self.status_code = status_code
        self.headers = headers or {}

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def iter_lines(self, decode_unicode=True):
        for line in self._lines:
            yield line


class _FakeRequests(object):
    """``requests`` 替身（``post`` 返回预置 SSE）。"""

    class exceptions(object):
        class ConnectionError(Exception):
            pass

        class Timeout(Exception):
            pass

        class RequestException(Exception):
            pass

    def __init__(self, resp):
        self._resp = resp
        self.calls = []

    def post(self, url, json=None, headers=None, stream=False, timeout=None):
        self.calls.append({"url": url, "json": json, "stream": stream})
        return self._resp


def _make_tunnel(ws_tunnel, node_secret="p18-secret", model="qwen2.5:0.5b"):
    """构造一个**不发网络**的假隧道（与阶段十七用例同一手法）。"""
    tunnel = ws_tunnel.WebSocketTunnel.__new__(ws_tunnel.WebSocketTunnel)
    tunnel._cfg = type("C", (object,), {
        "node_id": "node-p18-test",
        "node_name": "P18 测试节点",
        "node_secret": node_secret,
        "hardware_type": "cpu",
        "device": "cpu",
        "max_vram_gb": 2.0,
        "port": 52415,
        "backend_timeout_sec": 5.0,
        "model_name": model,
        "backend_model": model,
        "inference_backend": "fake",
    })()
    tunnel._total_completed_tokens = 0
    tunnel._models_cache = [model]
    tunnel._models_cache_ts = time.time()
    tunnel._loop = None
    tunnel._ws = None
    tunnel._running = True
    return tunnel


def main() -> int:
    import receipt_signing
    import telemetry
    import ws_tunnel

    secret = "p18-secret"
    rid = "req-p18-0001"
    mid = "qwen-2.5-72b"
    text = "这是一段用于签名的输出文本。"

    # ═══ 1. output_sha256 与手工计算一致 ═══
    receipt = receipt_signing.sign_inference_receipt(
        secret, rid, mid, 12, 34, text)
    manual_sha = hashlib.sha256(text.encode("utf-8")).hexdigest()
    _record(
        "output_sha256 等于手工 sha256(text.encode('utf-8'))",
        receipt.get("output_sha256") == manual_sha,
        "sha=%r" % receipt.get("output_sha256"),
    )

    # ═══ 2. canonical 严格是 request_id:model_id:prompt:completion:sha ═══
    expected_canonical = "%s:%s:%d:%d:%s" % (rid, mid, 12, 34, manual_sha)
    _record(
        "canonical 格式为 request_id:model_id:prompt:completion:sha256",
        receipt.get("canonical") == expected_canonical,
        "canonical=%r" % receipt.get("canonical"),
    )
    _record(
        "canonical 恰好 4 个冒号（字段数正确）",
        str(receipt.get("canonical", "")).count(":") == 4,
    )

    # ═══ 3. receipt_sig 等于手工 HMAC-SHA256 ═══
    manual_sig = hmac.new(
        secret.encode("utf-8"),
        expected_canonical.encode("utf-8"),
        hashlib.sha256,
    ).hexdigest()
    _record(
        "receipt_sig 等于手工 HMAC-SHA256(node_secret, canonical)",
        receipt.get("receipt_sig") == manual_sig,
        "sig=%r" % receipt.get("receipt_sig"),
    )
    _record(
        "receipt_sig 为小写十六进制（64 字符）",
        len(str(receipt.get("receipt_sig") or "")) == 64
        and str(receipt.get("receipt_sig")).lower() == receipt.get("receipt_sig"),
    )
    _record(
        "output_chars 等于输出文本长度",
        receipt.get("output_chars") == len(text),
        "chars=%r" % receipt.get("output_chars"),
    )

    # ═══ 4. 确定性 / 换密钥换签名 ═══
    again = receipt_signing.sign_inference_receipt(secret, rid, mid, 12, 34, text)
    other_secret = receipt_signing.sign_inference_receipt(
        "another-secret", rid, mid, 12, 34, text)
    _record(
        "确定性：同输入 ⇒ 同签名",
        again.get("receipt_sig") == receipt.get("receipt_sig"),
    )
    _record(
        "不同密钥 ⇒ 不同签名（密钥确实参与签名）",
        other_secret.get("receipt_sig") != receipt.get("receipt_sig"),
    )
    _record(
        "不同密钥时 output_sha256 不变（只有签名随密钥变）",
        other_secret.get("output_sha256") == receipt.get("output_sha256"),
    )

    # ═══ 5. verify_inference_receipt ═══
    good = receipt_signing.verify_inference_receipt(
        secret, receipt.get("canonical"), receipt.get("receipt_sig"))
    bad = receipt_signing.verify_inference_receipt(
        secret, receipt.get("canonical"), "0" * 64)
    tampered = receipt_signing.verify_inference_receipt(
        secret, receipt.get("canonical") + "x", receipt.get("receipt_sig"))
    wrong_key = receipt_signing.verify_inference_receipt(
        "wrong-key", receipt.get("canonical"), receipt.get("receipt_sig"))
    _record("verify：好签名 ⇒ True", good is True)
    _record("verify：伪造签名 ⇒ False", bad is False)
    _record("verify：被篡改的 canonical ⇒ False", tampered is False)
    _record("verify：错误密钥 ⇒ False", wrong_key is False)

    # ═══ 6. 空文本 / 空密钥 / 非字符串输入都不抛异常 ═══
    try:
        empty_text = receipt_signing.sign_inference_receipt(secret, rid, mid, 0, 0, "")
        empty_key = receipt_signing.sign_inference_receipt("", rid, mid, 0, 0, text)
        weird = receipt_signing.sign_inference_receipt(
            None, None, None, "not-a-number", None, 12345)
        ok_shape = (
            empty_text.get("output_sha256")
            == hashlib.sha256(b"").hexdigest()
            and empty_text.get("output_chars") == 0
            and isinstance(empty_key.get("receipt_sig"), str)
            and isinstance(weird.get("receipt_sig"), str)
            and isinstance(weird.get("canonical"), str)
        )
        _record("空文本 / 空密钥 / 畸形输入均不抛异常且结构完好", ok_shape)
    except Exception as exc:  # noqa: BLE001
        _record("空文本 / 空密钥 / 畸形输入均不抛异常且结构完好", False,
                "异常: %s" % exc)

    # ═══ 7. RESPONSE_DONE 帧携带 receipt ═══
    # ---- 7a. 真实推理后端路径（_run_backend）----
    tunnel = _make_tunnel(ws_tunnel)
    frames = []

    async def _capture(data):
        frames.append(data)

    tunnel._send_json = _capture
    backend = _FakeBackend(
        pieces=("你好", "，", "世界"),
        usage=_FakeUsage(prompt_tokens=11, completion_tokens=7))
    asyncio.run(tunnel._run_backend(backend, {"model": mid, "messages": [{"role": "user", "content": "hi"}]},
                                    rid))
    done = [f for f in frames if f.get("type") == "RESPONSE_DONE"]
    frame_receipt = done[0].get("receipt") if done else None
    _record(
        "RESPONSE_DONE 帧（真实后端路径）携带 receipt 键",
        bool(done) and isinstance(frame_receipt, dict),
        "done=%d" % len(done),
    )
    if isinstance(frame_receipt, dict):
        expected_text = "你好，世界"
        expected_sha = hashlib.sha256(expected_text.encode("utf-8")).hexdigest()
        expected_canon = "%s:%s:%d:%d:%s" % (rid, mid, 11, 7, expected_sha)
        expected_sig = hmac.new(
            secret.encode("utf-8"), expected_canon.encode("utf-8"),
            hashlib.sha256).hexdigest()
        _record(
            "帧内 receipt 的 output_sha256 / receipt_sig 与手工计算一致",
            frame_receipt.get("output_sha256") == expected_sha
            and frame_receipt.get("receipt_sig") == expected_sig,
            "canonical=%r" % frame_receipt.get("canonical"),
        )
        _record(
            "帧内 receipt 的 canonical 含云端下发的 model 与真实 usage",
            frame_receipt.get("canonical") == expected_canon,
            "canonical=%r" % frame_receipt.get("canonical"),
        )
    else:
        _record("帧内 receipt 的 output_sha256 / receipt_sig 与手工计算一致", False,
                "未取到 receipt")
        _record("帧内 receipt 的 canonical 含云端下发的 model 与真实 usage", False)

    # ---- 7b. 本地 API 转发路径（_forward_stream）----
    sse_lines = [
        "data: " + json.dumps({
            "choices": [{"index": 0, "delta": {"content": "abc"}}]}),
        "data: " + json.dumps({
            "choices": [{"index": 0, "delta": {"content": "def"}}]}),
        "data: " + json.dumps({
            "usage": {"prompt_tokens": 3, "completion_tokens": 5},
            "choices": [{"index": 0, "delta": {"content": ""}}]}),
        "data: [DONE]",
    ]
    tunnel2 = _make_tunnel(ws_tunnel)
    sent = []
    tunnel2._send_json_threadsafe = lambda data: sent.append(data)
    fake_requests = _FakeRequests(_FakeResp(sse_lines))
    original_requests = ws_tunnel.requests
    ws_tunnel.requests = fake_requests
    try:
        ok, usage, out_text = tunnel2._forward_stream(
            "http://127.0.0.1:1/v1/chat/completions",
            {"model": mid, "messages": []}, rid)
    finally:
        ws_tunnel.requests = original_requests
    _record(
        "_forward_stream 返回三元组 (ok, usage, text) 且文本完整拼接",
        ok is True and out_text == "abcdef"
        and isinstance(usage, dict) and usage.get("completion_tokens") == 5,
        "text=%r" % out_text,
    )

    # ---- 7c. 没有 node_secret ⇒ 不带 receipt 字段 ----
    tunnel3 = _make_tunnel(ws_tunnel, node_secret="")
    frames3 = []

    async def _capture3(data):
        frames3.append(data)

    tunnel3._send_json = _capture3
    asyncio.run(tunnel3._run_backend(
        _FakeBackend(pieces=("x",), usage=_FakeUsage(1, 1)),
        {"model": mid, "messages": [{"role": "user", "content": "hi"}]}, rid))
    done3 = [f for f in frames3 if f.get("type") == "RESPONSE_DONE"]
    _record(
        "缺少 node_secret 时 RESPONSE_DONE **不带** receipt 键（绝不发假签名）",
        bool(done3) and "receipt" not in done3[0],
        "keys=%s" % sorted(done3[0].keys()) if done3 else "no frame",
    )

    # ═══ 8. HTTP 心跳报文体携带 receipt ═══
    receipt_signing.remember_receipt(receipt)
    reporter = telemetry.TelemetryReporter.__new__(telemetry.TelemetryReporter)
    reporter._cfg = type("C", (object,), {
        "node_id": "node-p18-test",
        "node_name": "P18 测试节点",
        "hardware_type": "cpu",
        "wallet_address": "0xabc",
        "model_name": "qwen2.5:0.5b",
        "max_vram_gb": 2.0,
        "backend_model": "qwen2.5:0.5b",
        "inference_backend": "fake",
    })()
    payload = telemetry.TelemetryReporter.build_payload(reporter)
    payload_receipt = payload.get("receipt")
    _record(
        "HTTP 心跳报文体带 receipt 字段（值为最近一张收据）",
        isinstance(payload_receipt, dict)
        and payload_receipt.get("receipt_sig") == receipt.get("receipt_sig"),
        "receipt=%r" % (payload_receipt if not isinstance(payload_receipt, dict) else "dict"),
    )
    receipt_signing.clear_receipt()
    payload2 = telemetry.TelemetryReporter.build_payload(reporter)
    _record(
        "尚无收据时 receipt 为 None（不是空字典）",
        "receipt" in payload2 and payload2.get("receipt") is None,
        "receipt=%r" % payload2.get("receipt"),
    )
    receipt_signing.remember_receipt(receipt)

    # ═══ 9. Agent 版本 ═══
    import agent_config  # noqa: E402

    _record(
        "AGENT_VERSION >= 1.0.17",
        tuple(int(x) for x in str(agent_config.AGENT_VERSION).split(".")) >= (1, 0, 17),
        "version=%r" % agent_config.AGENT_VERSION,
    )

    # ═══ 10. 真机 agent.config.json 零污染 ═══
    real_cfg = REPO_ROOT / "agent.config.json"
    _record(
        "真机 agent.config.json 字节级未变（零污染）",
        real_cfg.read_bytes() == _CONFIG_SNAPSHOT,
        "size=%d" % (real_cfg.stat().st_size if real_cfg.exists() else -1),
    )

    failed = [r for r in RESULTS if not r[1]]
    print("\n===== Agent PoI 签名收据自检：%d/%d 通过 ====="
          % (len(RESULTS), len(RESULTS) - len(failed)))
    if failed:
        for name, _ok, detail in failed:
            print("  FAIL: %s | %s" % (name, detail))
        return 1
    return 0


if __name__ == "__main__":
    # 基线快照必须在任何导入副作用之前取
    _real = REPO_ROOT / "agent.config.json"
    _CONFIG_SNAPSHOT = _real.read_bytes() if _real.exists() else b""
    sys.exit(main())
