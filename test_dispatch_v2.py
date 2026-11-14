"""阶段五 P4 · 模块 B：Agent 侧派单协议 v2 自检（盲派单载荷 + 老版本兼容）。

用法（在仓库根目录执行）：
    ./.venv/Scripts/python.exe test_dispatch_v2.py

覆盖用例：
  1. INIT 帧声明 ``protocol_version: 2``（云端据此只下发加密模型指令）
  2. 老载荷（明文 ``model``）原样透传 —— **老版本云端完全兼容**
  3. v2 载荷（``model_cipher``）能用本节点密钥解出模型名
  4. 密钥不匹配 ⇒ 返回 None（调用方回 ``model_decrypt_failed``），
     **绝不**拿着乱码模型名去跑推理
  5. 加解密往返 + 篡改检出（与云端同一套算法）

测试完全离线：不连生产云端。
"""

import os
import sys

os.environ["APIPAW_DISABLE_TELEMETRY"] = "1"

REPO_DIR = os.path.dirname(os.path.abspath(__file__))
if REPO_DIR not in sys.path:
    sys.path.insert(0, REPO_DIR)

RESULTS = []


def _record(name, ok, detail=""):
    RESULTS.append((name, ok, detail))
    print(("[PASS] " if ok else "[FAIL] ") + name + (" | " + detail if detail else ""))


class _FakeMonitor(object):
    """最小 monitor 替身 —— 只提供 ``_build_frame`` 用到的两个属性。"""

    state = type("S", (object,), {"name": "IDLE"})()
    state_label = "空闲"


def _make_tunnel(ws_tunnel, node_secret):
    tunnel = ws_tunnel.WebSocketTunnel.__new__(ws_tunnel.WebSocketTunnel)
    tunnel._cfg = type("C", (object,), {
        "node_id": "node-p4-test",
        "node_name": "P4 测试节点",
        "hardware_type": "cuda",
        "device": "cuda",
        "max_vram_gb": 4.5,
    })()
    tunnel._total_completed_tokens = 0
    tunnel._node_secret = node_secret
    return tunnel


def main():
    import dispatch_crypto
    import telemetry
    import ws_tunnel

    secret = "node-secret-for-test-0123456789"
    model_id = "qwen-2.5-72b"

    # ---- 1. INIT 帧声明协议版本 ----
    telemetry.collect_gpu_metrics_cached = lambda: {
        "gpu_temp_c": 50.0, "vram_used_gb": 3.0,
        "vram_total_gb": 24.0, "vram_free_gb": 21.0,
    }
    ws_tunnel.collect_gpu_metrics_cached = telemetry.collect_gpu_metrics_cached
    tunnel = _make_tunnel(ws_tunnel, secret)
    frame = tunnel._build_frame("INIT", _FakeMonitor())
    _record("INIT 帧声明 protocol_version=2",
            frame.get("protocol_version") == 2,
            "protocol_version=%s" % frame.get("protocol_version"))
    _record("INIT 帧保留既有字段（app_ping / agent_version / node_id）",
            frame.get("app_ping") == 1 and bool(frame.get("agent_version"))
            and frame.get("node_id") == "node-p4-test")

    # ---- 2. 老载荷原样透传（老版本云端兼容） ----
    legacy = {"model": model_id, "messages": [], "stream": True}
    resolved = tunnel._resolve_dispatch_model(legacy)
    _record("老载荷（明文 model）原样透传",
            resolved is not None and resolved.get("model") == model_id,
            str(resolved))
    _record("老载荷不被篡改（无多余字段）",
            resolved is not None and "model_cipher" not in resolved)

    # ---- 3. v2 载荷解密 ----
    cipher = dispatch_crypto.encrypt_model(model_id, secret)
    v2_payload = {"model_ref": "m-1234abcd", "capability_class": "C1",
                  "protocol_version": 2, "model_cipher": cipher,
                  "messages": [], "stream": True}
    resolved = tunnel._resolve_dispatch_model(v2_payload)
    _record("v2 载荷解出模型名", resolved is not None and resolved.get("model") == model_id,
            str(resolved))
    _record("v2 解密后不再携带密文",
            resolved is not None and "model_cipher" not in resolved)

    # ---- 4. 密钥不匹配 ⇒ None（回 model_decrypt_failed） ----
    wrong = _make_tunnel(ws_tunnel, "another-secret")
    resolved_bad = wrong._resolve_dispatch_model(v2_payload)
    _record("密钥不匹配时返回 None（不跑错模型）", resolved_bad is None, str(resolved_bad))

    # ---- 5. 加解密往返 + 篡改检出 ----
    again = dispatch_crypto.decrypt_model(
        dispatch_crypto.encrypt_model(model_id, secret), secret)
    _record("加解密往返一致", again == model_id, again)
    try:
        dispatch_crypto.decrypt_model(cipher[:-2] + "00", secret)
        _record("篡改密文被检出", False, "竟然解开了")
    except ValueError as exc:
        _record("篡改密文被检出", True, str(exc)[:50])

    passed = sum(1 for _, ok, _ in RESULTS if ok)
    total = len(RESULTS)
    print("\n===== 阶段五 P4 · 模块 B（Agent 侧）沙箱：%d/%d 通过 =====" % (passed, total))
    if passed != total:
        for name, ok, detail in RESULTS:
            if not ok:
                print("  FAIL: %s | %s" % (name, detail))
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
