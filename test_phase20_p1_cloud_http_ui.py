"""
test_phase20_p1_cloud_http_ui.py — v1.0.19 真机 Bug A/B/C 的回归守卫

覆盖三件事（**全部零网络、零真实端口**）：

A. ``/ui`` 面板布局：CSS 必须是「顶部对齐 + 可滚动」，不能是垂直居中。
B. URL 预填与绑定：``node_id`` / ``node_secret`` **同时**预填、
   轮询不得回写、存在「立即保存绑定」快捷按钮。
C. 云端请求：必须带合规 UA（否则 Cloudflare 403）、短超时、
   退避重试、缓存兜底。

⚠️ 本测试**不连任何真实地址**：``cloud_http`` 的 ``opener`` 参数可注入，
这里一律注入假 ``urlopen``。
"""

from __future__ import annotations

import io
import json
import os
import re
import sys
import urllib.error
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(REPO_ROOT))

import cloud_http  # noqa: E402
import web_ui  # noqa: E402

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


class _FakeResp:
    """最小可用的 urlopen 返回值。"""

    def __init__(self, payload: bytes = b"") -> None:
        self._payload = payload

    def __enter__(self) -> "_FakeResp":
        return self

    def __exit__(self, *exc: object) -> bool:
        return False

    def read(self) -> bytes:
        return self._payload


# ── Bug C：云端请求头 / 重试 / 缓存 ──────────────────────────────

def _case_c_user_agent() -> None:
    """请求必须带合规 UA（默认 Python-urllib UA 会被 Cloudflare 403）。"""
    seen = {}

    def _opener(req, timeout=None):
        seen["ua"] = (req.headers or {}).get("User-agent") or ""
        seen["accept"] = (req.headers or {}).get("Accept") or ""
        seen["timeout"] = timeout
        return _FakeResp(b'{"ok": true}')

    cloud_http.fetch_cloud_json("https://cloud.example.com/x", opener=_opener)
    ua = seen.get("ua", "")
    _record(
        "C1 云端请求带合规 UA（不是 Python-urllib）",
        ua.startswith("Mozilla/5.0") and "APIPawAgent" in ua
        and "Python-urllib" not in ua,
        "UA=%r" % ua,
    )
    _record(
        "C2 云端请求带 Accept + 短超时",
        seen.get("accept") == "application/json"
        and float(seen.get("timeout") or 0) <= cloud_http.DEFAULT_TIMEOUT_SEC,
        "Accept=%r timeout=%r" % (seen.get("accept"), seen.get("timeout")),
    )


def _case_c_retry() -> None:
    """链路抖动要退避重试；业务 4xx 不重试。"""
    calls = {"n": 0}

    def _flaky(req, timeout=None):
        calls["n"] += 1
        if calls["n"] < 3:
            raise urllib.error.URLError("模拟 TLS 握手超时")
        return _FakeResp(b'{"models": []}')

    cloud_http.fetch_cloud_json("https://cloud.example.com/retry", opener=_flaky)
    _record("C3 链路失败自动重试并最终成功", calls["n"] == 3, "调用 %d 次" % calls["n"])

    hard = {"n": 0}

    def _business_error(req, timeout=None):
        hard["n"] += 1
        raise urllib.error.HTTPError(req.full_url, 404, "Not Found", None, None)

    try:
        cloud_http.fetch_cloud_json("https://cloud.example.com/404", opener=_business_error)
        ok = False
    except urllib.error.HTTPError:
        ok = True
    _record("C4 业务 4xx 不重试（立即抛出）", ok and hard["n"] == 1, "调用 %d 次" % hard["n"])


def _case_c_cache() -> None:
    """网络失败时回落缓存（面板不至于整块变空）。"""
    url = "https://cloud.example.com/cached"
    cloud_http._CACHE.pop(url, None)

    def _good(req, timeout=None):
        return _FakeResp(b'{"golden_count": 7}')

    data, stale = cloud_http.fetch_cloud_json_cached(url, opener=_good)
    _record("C5 首次拉取成功且非过期", data.get("golden_count") == 7 and stale is False,
            "data=%r stale=%r" % (data, stale))

    def _bad(req, timeout=None):
        raise urllib.error.URLError("模拟断网")

    try:
        data2, stale2 = cloud_http.fetch_cloud_json_cached(url, opener=_bad)
        ok = data2.get("golden_count") == 7 and stale2 is True
    except Exception as exc:  # noqa: BLE001
        ok = False
        data2 = repr(exc)
    _record("C6 网络失败时回落缓存并标注 stale", ok, "data=%r" % (data2,))


# ── Bug A / B：面板 HTML ──────────────────────────────────────────

def _case_a_layout() -> None:
    """面板必须顶部对齐 + 可滚动（不能垂直居中把顶部顶出视口）。"""
    html = web_ui.UI_HTML
    body_match = re.search(r"\n\s*body\s*\{(.*?)\}", html, re.S)
    body_css = body_match.group(1) if body_match else ""
    _record(
        "A1 body 不再使用 align-items: center（垂直居中会把顶部顶出视口）",
        "align-items: center" not in body_css and "align-items: flex-start" in body_css,
        "body CSS 片段=%r" % body_css.strip()[:120],
    )
    _record(
        "A2 body 允许纵向滚动",
        "overflow-y: auto" in body_css,
        "overflow-y: auto in body=%r" % ("overflow-y: auto" in body_css),
    )
    _record(
        "A3 html 不再用 height:100% 卡死滚动",
        not re.search(r"html,\s*body\s*\{\s*height:\s*100%", html),
        "html,body{height:100%} 已移除",
    )
    _record(
        "A4 .wrap 水平居中（margin:auto）",
        re.search(r"\.wrap\s*\{[^}]*margin:\s*0 auto", html, re.S) is not None,
        ".wrap margin:0 auto",
    )


def _case_b_prefill() -> None:
    """URL 预填：两个字段都填、轮询不回写、有快捷绑定按钮。"""
    html = web_ui.UI_HTML
    _record(
        "B1 存在预填提示条与「立即保存绑定」按钮",
        'id="prefillBanner"' in html and 'id="prefillBindBtn"' in html
        and "立即保存绑定" in html,
        "prefillBanner/prefillBindBtn 均在",
    )
    _record(
        "B2 预填标志位 hasUrlPrefill 存在",
        "hasUrlPrefill" in html,
        "hasUrlPrefill",
    )
    _record(
        "B3 轮询不得回写预填/用户编辑过的输入框",
        re.search(r"if\s*\(\s*!hasUrlPrefill\s*&&\s*!userEdited\s*&&", html) is not None,
        "轮询条件含 !hasUrlPrefill && !userEdited",
    )
    _record(
        "B4 node_secret 与 node_id 一同预填",
        re.search(r"if\s*\(\s*qid\s*\)\s*\{\s*nodeIdEl\.value\s*=\s*qid", html) is not None
        and re.search(r"if\s*\(\s*qsec\s*\)\s*\{\s*secretEl\.value\s*=\s*qsec", html) is not None,
        "两个字段分别赋值",
    )
    _record(
        "B5 两个参数齐备时自动绑定（doBind(true)）",
        re.search(r"doBind\(true\)", html) is not None,
        "setTimeout → doBind(true)",
    )
    _record(
        "B6 地址栏敏感参数被清除（history.replaceState）",
        "history.replaceState" in html,
        "replaceState 已存在",
    )
    _record(
        "B7 预填后主动滚回顶部",
        "window.scrollTo(0, 0)" in html,
        "scrollTo(0,0)",
    )


def _case_b_bind_api() -> None:
    """/api/bind 接口本身仍然正常（回归）。"""
    from asgi_test_client import TestClient  # noqa: E402

    import agent_config
    import main as agent_main

    tmp_dir = Path(os.environ.get("TEMP", "/tmp")) / "apipaw_p20_p1_ui"
    if not tmp_dir.is_dir():
        tmp_dir.mkdir(parents=True, exist_ok=True)
    path = tmp_dir / "agent.config.json"
    cfg = agent_config.AgentConfig(config_path=path)
    agent_config._config_instance = cfg  # type: ignore[attr-defined]
    agent_main.get_config = lambda: cfg  # type: ignore[assignment]

    client = TestClient(agent_main.create_app())
    resp = client.post("/api/bind", json={
        "node_id": "node_test_v1019",
        "node_secret": "secret_test_v1019",
    })
    body = resp.json() if resp.content else {}
    _record(
        "B8 /api/bind 仍可保存凭证",
        resp.status_code == 200 and bool(body.get("ok")),
        "status=%s body=%r" % (resp.status_code, body),
    )


def main() -> int:
    _case_c_user_agent()
    _case_c_retry()
    _case_c_cache()
    _case_a_layout()
    _case_b_prefill()
    _case_b_bind_api()

    print("=" * 56)
    print("v1.0.19 真机 Bug A/B/C 回归：%d 通过 / %d 失败" % (_PASS, _FAIL))
    return 0 if _FAIL == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
