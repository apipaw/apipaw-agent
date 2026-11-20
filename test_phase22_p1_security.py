"""
test_phase22_p1_security.py — 阶段二十二 P1 · Agent 本地面板纵深加固的回归守卫

覆盖五件事（**零网络、零真实端口、零生产配置**）：

A. **Host 闸门**（``TrustedHostMiddleware``）
   - ``Host: 127.0.0.1`` / ``localhost`` ⇒ 放行；
   - ``Host: evil.example.com`` ⇒ **400**（DNS Rebinding 防线）；
   - 端口后缀（``127.0.0.1:52415``）不影响判定。

B. **Origin 闸门**（写端点）
   - 本地面板来源（``http://127.0.0.1:52415`` / ``http://localhost:52415``）⇒ 放行；
   - 恶意来源（``https://evil.example.com``）⇒ **403**；
   - ``Sec-Fetch-Site: cross-site`` ⇒ **403**（哪怕 Origin 缺失）；
   - 无 Origin（CLI / 测试客户端）⇒ 放行。

C. **取消自动绑定**
   - ``web_ui.py`` 里**不得**再有「URL 带参 ⇒ 自动 doBind」的代码路径；
   - 预填 + 提示条 + 「立即保存绑定」按钮必须仍在。

D. **备用网关白名单**
   - 只接受 ``wss://`` / ``https://``；
   - 域名必须命中信任域（默认 ``*.apipaw.com``，可用
     ``APIPAW_TRUSTED_GATEWAY_DOMAINS`` 扩展）；
   - ``http://`` / ``ws://`` / ``https://evil.com`` 一律拒绝；
   - ``agent_config.backup_gateway_urls`` 属性本身也要过滤（手工改配置也挡得住）。

E. **端点级行为**
   - ``/api/bind`` 带恶意 Origin ⇒ 403；
   - ``/api/mesh/prefs`` 写入不可信网关地址 ⇒ 400。

跑法（仓库根目录）：
    python test_phase22_p1_security.py
"""

from __future__ import annotations

import io
import os
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(REPO_ROOT))

import local_guard  # noqa: E402
import gateway_failover  # noqa: E402

_PASS = 0
_FAIL = 0


def _record(name: str, ok: bool, detail: str = "") -> None:
    global _PASS, _FAIL
    if ok:
        _PASS += 1
        print("[PASS] %s | %s" % (name, detail))
    else:
        _FAIL += 1
        print("[FAIL] %s | %s" % (name, detail))


def _make_app():
    """构造 app（**不进 lifespan** ⇒ 不会连生产云端、不会起 WS 隧道）。"""
    import main
    return main.create_app()


def _case_a_host_guard() -> None:
    from asgi_test_client import TestClient

    app = _make_app()
    client = TestClient(app)

    ok_local = client.get("/health", headers={"host": "127.0.0.1"})
    _record("A1. Host: 127.0.0.1 ⇒ 放行", ok_local.status_code != 400,
            "HTTP %d" % ok_local.status_code)

    ok_name = client.get("/health", headers={"host": "localhost"})
    _record("A2. Host: localhost ⇒ 放行", ok_name.status_code != 400,
            "HTTP %d" % ok_name.status_code)

    ok_port = client.get("/health", headers={"host": "127.0.0.1:52415"})
    _record("A3. Host: 127.0.0.1:52415（带端口）⇒ 放行",
            ok_port.status_code != 400, "HTTP %d" % ok_port.status_code)

    evil = client.get("/health", headers={"host": "evil.example.com"})
    _record("A4. ⛔ Host: evil.example.com ⇒ 400（防 DNS Rebinding）",
            evil.status_code == 400, "HTTP %d" % evil.status_code)

    evil_post = client.post("/api/bind", json={"node_id": "x", "node_secret": "y"},
                             headers={"host": "evil.example.com"})
    _record("A5. ⛔ Host 非法时写端点同样被挡（400）",
            evil_post.status_code == 400, "HTTP %d" % evil_post.status_code)


def _case_b_origin_guard() -> None:
    origins = local_guard.allowed_origins("127.0.0.1", 52415)
    ok_local, _ = local_guard.check_request_origin(
        {"origin": "http://127.0.0.1:52415"}, origins)
    _record("B1. 本地面板 Origin（127.0.0.1）⇒ 放行", ok_local, str(origins))

    ok_local2, _ = local_guard.check_request_origin(
        {"origin": "http://localhost:52415"}, origins)
    _record("B2. 本地面板 Origin（localhost）⇒ 放行", ok_local2, "")

    bad, reason = local_guard.check_request_origin(
        {"origin": "https://evil.example.com"}, origins)
    _record("B3. ⛔ 恶意 Origin ⇒ 拒绝", not bad, "reason=%s" % reason)

    bad2, reason2 = local_guard.check_request_origin(
        {"origin": "http://127.0.0.1:52415", "sec-fetch-site": "cross-site"}, origins)
    _record("B4. ⛔ Sec-Fetch-Site: cross-site ⇒ 拒绝（即便 Origin 合法）",
            not bad2, "reason=%s" % reason2)

    none_ok, reason3 = local_guard.check_request_origin({}, origins)
    _record("B5. 无 Origin（CLI / 测试客户端）⇒ 放行", none_ok, "reason=%s" % reason3)

    # 写端点清单必须覆盖所有能改配置的端点
    for path in ("/api/bind", "/api/prefs", "/api/mesh/prefs",
                 "/api/ollama/pull", "/api/ollama/evolve"):
        _record("B6. 写端点清单覆盖 %s" % path,
                path in local_guard.LOCAL_WRITE_PATHS, "")


def _case_c_no_auto_bind() -> None:
    import web_ui

    src = io.open(str(REPO_ROOT / "web_ui.py"), encoding="utf-8").read()
    # 自动绑定的特征是**代码里**出现 doBind(true) / setTimeout(…doBind…)；
    # 注释里提到它是允许的（历史说明），所以只扫「非注释代码行」。
    code_lines = []
    for raw in src.splitlines():
        line = raw.strip()
        if not line or line.startswith("//") or line.startswith("*") \
                or line.startswith("/*") or line.startswith("<!--"):
            continue
        code_lines.append(line)
    code = "\n".join(code_lines)
    auto = "doBind(true)" in code or "setTimeout(function () { doBind" in code
    _record("C1. ⛔ web_ui.py 不再有「URL 带参 ⇒ 自动绑定」调用",
            not auto, "代码行里 doBind(true) x%d" % code.count("doBind(true)"))
    _record("C1b. 绑定只由用户点击触发（两个按钮都是 doBind(false)）",
            code.count("doBind(false)") >= 2,
            "doBind(false) x%d" % code.count("doBind(false)"))
    _record("C2. 预填逻辑仍在（URL 参数仍会填进输入框）",
            "node_secret" in src and "prefillFromQuery" in src, "")
    _record("C3. 「立即保存绑定」快捷按钮仍在",
            "立即保存绑定" in src, "")
    _record("C4. 面板仍含提示条（banner）",
            "bannerEl" in web_ui.UI_HTML, "")


def _case_d_gateway_allowlist() -> None:
    ok = gateway_failover.is_trusted_gateway_url("https://backup.apipaw.com")
    _record("D1. https://backup.apipaw.com ⇒ 受信", ok, "")

    ok2 = gateway_failover.is_trusted_gateway_url("wss://backup.apipaw.com")
    _record("D2. wss://backup.apipaw.com ⇒ 受信", ok2, "")

    bad = gateway_failover.is_trusted_gateway_url("http://backup.apipaw.com")
    _record("D3. ⛔ http://（明文）⇒ 拒绝", not bad, "")

    bad2 = gateway_failover.is_trusted_gateway_url("ws://backup.apipaw.com")
    _record("D4. ⛔ ws://（明文）⇒ 拒绝", not bad2, "")

    bad3 = gateway_failover.is_trusted_gateway_url("https://evil.example.com")
    _record("D5. ⛔ 非信任域 ⇒ 拒绝", not bad3, "")

    bad4 = gateway_failover.is_trusted_gateway_url("https://apipaw.com.evil.com")
    _record("D6. ⛔ 后缀伪装（apipaw.com.evil.com）⇒ 拒绝", not bad4, "")

    kept = gateway_failover.validate_backup_gateway_urls([
        "https://backup.apipaw.com", "http://evil.com", "wss://a.apipaw.com",
    ])
    _record("D7. 批量过滤只留受信项",
            kept == ["https://backup.apipaw.com", "wss://a.apipaw.com"], str(kept))

    # 环境变量扩展信任域
    old = os.environ.get("APIPAW_TRUSTED_GATEWAY_DOMAINS")
    try:
        os.environ["APIPAW_TRUSTED_GATEWAY_DOMAINS"] = "example.com"
        ext = gateway_failover.is_trusted_gateway_url("https://gw.example.com")
        _record("D8. APIPAW_TRUSTED_GATEWAY_DOMAINS 可扩展信任域", ext, "")
        still_bad = gateway_failover.is_trusted_gateway_url("https://evil.com")
        _record("D9. 扩展后未列出的域名仍然被拒", not still_bad, "")
    finally:
        if old is None:
            os.environ.pop("APIPAW_TRUSTED_GATEWAY_DOMAINS", None)
        else:
            os.environ["APIPAW_TRUSTED_GATEWAY_DOMAINS"] = old

    # agent_config 属性层过滤（**手工改 agent.config.json 也挡得住**）
    # 用临时配置文件，绝不碰真机 agent.config.json。
    import shutil
    import tempfile

    import agent_config

    tmp_dir = tempfile.mkdtemp(prefix="apipaw_p22_")
    try:
        tmp_path = Path(tmp_dir) / "agent.config.json"
        isolated = agent_config.AgentConfig(config_path=tmp_path)
        isolated.update(backup_gateway_urls=[
            "https://backup.apipaw.com", "https://evil.example.com",
        ])
        filtered = list(isolated.backup_gateway_urls)
        _record("D10. agent_config 属性层过滤不可信网关",
                filtered == ["https://backup.apipaw.com"], str(filtered))
    finally:
        shutil.rmtree(tmp_dir, ignore_errors=True)


def _case_e_endpoints() -> None:
    from asgi_test_client import TestClient

    app = _make_app()
    client = TestClient(app)

    denied = client.post("/api/bind", json={"node_id": "x", "node_secret": "y"},
                          headers={"host": "127.0.0.1",
                                   "origin": "https://evil.example.com"})
    _record("E1. ⛔ /api/bind 带恶意 Origin ⇒ 403",
            denied.status_code == 403, "HTTP %d" % denied.status_code)

    denied2 = client.post("/api/prefs", json={"force_idle": True},
                           headers={"host": "127.0.0.1",
                                    "origin": "https://evil.example.com"})
    _record("E2. ⛔ /api/prefs 带恶意 Origin ⇒ 403",
            denied2.status_code == 403, "HTTP %d" % denied2.status_code)

    denied3 = client.post("/api/mesh/prefs",
                           json={"backup_gateway_urls": ["https://evil.example.com"]},
                           headers={"host": "127.0.0.1",
                                    "origin": "http://127.0.0.1:52415"})
    _record("E3. ⛔ /api/mesh/prefs 写入不可信网关 ⇒ 400",
            denied3.status_code == 400, "HTTP %d" % denied3.status_code)


def main() -> int:
    _case_a_host_guard()
    _case_b_origin_guard()
    _case_c_no_auto_bind()
    _case_d_gateway_allowlist()
    _case_e_endpoints()
    print("\n=== 阶段二十二 P1 · Agent 安全加固自检: %d 通过 / %d 失败 ==="
          % (_PASS, _FAIL))
    return 0 if _FAIL == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
