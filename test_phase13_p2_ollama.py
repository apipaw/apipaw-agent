"""
test_phase13_p2_ollama.py —— 阶段十三 P2「免命令行一键拉取 Ollama 模型」回归自检

背景
----
阶段十三 P2 要消灭节点运营者最后一个手工步骤：以前要装一个模型接单，只能

1. 打开命令行敲 ``ollama pull qwen2.5:0.5b``；
2. 或者手工改 ``agent.config.json`` 的 ``backend_model``。

现在面板上一个区块搞定：本机已装模型 + 云端推荐列表 + 「一键拉取并绑定」。

本文件锁死七条不变量：

- **P1** 模型名白名单：拒绝空名 / ``..`` 路径穿越 / 前导斜杠 / 空格 / 引号；
- **P2** ``GET /api/ollama/tags`` 返回本机已装模型 + 在线状态 + 拉取进度；
  **Ollama 没起来不算错误**（``online=False`` + 原因，绝不 5xx）；
- **P3** ``POST /api/ollama/pull`` 非法模型名 ⇒ 400（绝不把脏名字拼进 URL）；
- **P4** 拉取**成功**后自动把 ``backend_model`` + ``model_name`` 写进配置
  （``model_name`` 不改的话云端匹配不上这个节点 ⇒ 永远派不到单）；
- **P5** ``POST /api/ollama/bind`` 把已装模型设为当前接单模型；
  没装的模型 ⇒ 明确拒绝（不许绑一个跑不起来的模型）；
- **P6** ``GET /api/ollama/recommended`` 代理云端推荐列表（面板不直连云端）；
- **P7** 全程**绝不触碰真机 ``agent.config.json``**（用临时目录配置 + 前后字节比对）。

⚠️ 本机 Ollama 全部用**假 urlopen** 替换 —— 不下载任何东西、不连任何真实端口。
"""

from __future__ import annotations

import json
import sys
import tempfile
import time
from pathlib import Path
from unittest import mock

REPO_ROOT = Path(__file__).resolve().parents[0]
sys.path.insert(0, str(REPO_ROOT))

import agent_config  # noqa: E402
import main as agent_main  # noqa: E402
import ollama_local  # noqa: E402

_PASS = 0
_FAIL = 0

TAGS_BODY = {
    "models": [
        {
            "name": "qwen2.5:0.5b",
            "size": 397316096,
            "details": {"family": "qwen2", "parameter_size": "0.5B"},
        }
    ]
}

RECOMMENDED_BODY = {
    "models": [
        {
            "model_id": "qwen2.5:0.5b",
            "display_name": "Qwen2.5 0.5B",
            "min_vram_gb": 1.5,
            "prompt_price_per_1k": 0.001,
            "completion_price_per_1k": 0.002,
            "royalty_rate": 0.15,
            "online_nodes": 2,
            "recommended": True,
        }
    ],
    "count": 1,
}


def _record(name: str, ok: bool, detail: str = "") -> None:
    global _PASS, _FAIL
    if ok:
        _PASS += 1
        print("[PASS] %s | %s" % (name, detail))
    else:
        _FAIL += 1
        print("[FAIL] %s | %s" % (name, detail))


class _FakeResp:
    """假 HTTP 响应：既支持 ``read()``（整包 JSON）也支持逐行迭代（NDJSON 流）。"""

    def __init__(self, payload: bytes = b"", lines=None) -> None:
        self._payload = payload
        self._lines = lines or []

    def __enter__(self) -> "_FakeResp":
        return self

    def __exit__(self, *exc: object) -> bool:
        return False

    def read(self) -> bytes:
        return self._payload

    def __iter__(self):
        return iter(self._lines)


def _fake_urlopen(req, timeout=None):
    url = getattr(req, "full_url", "") or getattr(req, "url", "")
    if url.endswith("/api/pull"):
        return _FakeResp(lines=[
            b'{"status":"pulling manifest"}\n',
            b'{"status":"success","completed":100,"total":100}\n',
        ])
    if url.endswith("/api/tags"):
        return _FakeResp(payload=json.dumps(TAGS_BODY).encode("utf-8"))
    if url.endswith("/api/v1/network/recommended-models"):
        return _FakeResp(payload=json.dumps(RECOMMENDED_BODY).encode("utf-8"))
    raise AssertionError("测试里出现了意料之外的 URL: %s" % url)


def _make_config(tmp_dir: str):
    """造一份指向临时文件的配置实例（**不碰用户真实的 agent.config.json**）。"""
    path = Path(tmp_dir) / "agent.config.json"
    cfg = agent_config.AgentConfig(config_path=path)
    return cfg, path


def main() -> int:
    # ⚠️ 阶段十六 P0：用**零依赖**的自研 ASGI 客户端，不再依赖 httpx
    from asgi_test_client import TestClient  # noqa: E402

    tmp_dir = tempfile.mkdtemp(prefix="apipaw_p13_p2_")
    cfg, cfg_path = _make_config(tmp_dir)

    # 让所有模块都读这份临时配置（阶段八 P0：显式 config_path ⇒ 独立实例）
    agent_config._config_instance = cfg  # type: ignore[attr-defined]
    agent_main.get_config = lambda: cfg  # type: ignore[assignment]

    # 真机配置的前后字节（用于 P7 断言"绝不触碰"）
    real_cfg = REPO_ROOT / "agent.config.json"
    before = real_cfg.read_bytes() if real_cfg.exists() else b""

    # ── P1：模型名白名单 ──
    ok_names = ["qwen2.5:0.5b", "llama3", "library/llama3:8b", "qwen2_5-instruct"]
    _record(
        "P1 合法模型名通过校验",
        all(ollama_local.validate_model_name(n) == n for n in ok_names),
        "ok_names=%r" % (ok_names,),
    )
    bad_names = ["", "   ", "../etc/passwd", "/abs/model", "model/", "a b",
                 'qwen"x', "a" * 200, "ns/model:tag/extra"]
    rejected = []
    for name in bad_names:
        try:
            ollama_local.validate_model_name(name)
        except ValueError:
            rejected.append(name)
    _record(
        "P1 非法模型名全部被拒",
        len(rejected) == len(bad_names),
        "rejected=%d/%d" % (len(rejected), len(bad_names)),
    )

    app = agent_main.create_app()
    # ⚠️ **不进 lifespan**（不进 ``with``）：lifespan 会启动 WS 隧道 + 遥测
    #    ⇒ 测试会真的连上生产云端并上报心跳。这里只测纯 HTTP 端点。
    client = TestClient(app)

    with mock.patch("urllib.request.urlopen", side_effect=_fake_urlopen):
        # ── P2：GET /api/ollama/tags ──
        tags = client.get("/api/ollama/tags").json()
        _record(
            "P2 GET /api/ollama/tags 返回本机模型与在线状态",
            tags.get("ok") is True
            and tags.get("online") is True
            and len(tags.get("models") or []) == 1
            and (tags.get("models") or [{}])[0].get("name") == "qwen2.5:0.5b",
            "online=%r models=%r" % (tags.get("online"), tags.get("models")),
        )
        _record(
            "P2 /api/ollama/tags 带拉取进度与当前模型",
            isinstance(tags.get("pull"), dict) and "current_model" in tags,
            "pull=%r current_model=%r" % (tags.get("pull"), tags.get("current_model")),
        )

        # ── P3：非法模型名 ⇒ 400 ──
        bad = client.post("/api/ollama/pull", json={"model": "../evil"})
        _record(
            "P3 POST /api/ollama/pull 非法模型名 ⇒ 400",
            bad.status_code == 400 and bad.json().get("ok") is False,
            "HTTP %s body=%s" % (bad.status_code, bad.text),
        )
        _record(
            "P3 非法模型名没有污染配置",
            cfg.backend_model == "" and cfg.model_name != "../evil",
            "backend_model=%r" % cfg.backend_model,
        )

        # ── P4：拉取成功 ⇒ 自动写盘 backend_model + model_name ──
        pull = client.post("/api/ollama/pull", json={"model": "qwen2.5:0.5b"})
        _record(
            "P4 POST /api/ollama/pull 已触发",
            pull.status_code == 200 and pull.json().get("started") is True,
            "HTTP %s body=%s" % (pull.status_code, pull.text),
        )
        # 拉取在**后台线程**里跑 ⇒ 轮询到状态离开 running
        state = ""
        deadline = time.time() + 10.0
        while time.time() < deadline:
            st = client.get("/api/ollama/tags").json()
            state = str((st.get("pull") or {}).get("state") or "")
            if state and state != "running":
                break
            time.sleep(0.2)
        _record(
            "P4 拉取线程跑完（state=done）",
            state == "done",
            "state=%r" % state,
        )
        saved = json.loads(cfg_path.read_text(encoding="utf-8"))
        _record(
            "P4 拉取成功后 backend_model 已写盘",
            saved.get("backend_model") == "qwen2.5:0.5b",
            "saved.backend_model=%r" % saved.get("backend_model"),
        )
        _record(
            "P4 拉取成功后 model_name 已写盘（云端靠它认节点）",
            saved.get("model_name") == "qwen2.5:0.5b",
            "saved.model_name=%r" % saved.get("model_name"),
        )

        # ── P5：POST /api/ollama/bind ──
        bind = client.post("/api/ollama/bind", json={"model": "qwen2.5:0.5b"})
        _record(
            "P5 POST /api/ollama/bind 已装模型可绑定",
            bind.status_code == 200 and bind.json().get("ok") is True,
            "HTTP %s body=%s" % (bind.status_code, bind.text),
        )
        not_installed = client.post("/api/ollama/bind", json={"model": "llama3:70b"})
        _record(
            "P5 未安装的模型被拒（400）",
            not_installed.status_code == 400,
            "HTTP %s body=%s" % (not_installed.status_code, not_installed.text),
        )
        _record(
            "P5 绑定失败没有改动 model_name",
            cfg.model_name == "qwen2.5:0.5b",
            "model_name=%r" % cfg.model_name,
        )

        # ── P6：GET /api/ollama/recommended 代理云端 ──
        rec = client.get("/api/ollama/recommended").json()
        items = rec.get("models") or []
        _record(
            "P6 GET /api/ollama/recommended 透传云端推荐",
            rec.get("ok") is True and len(items) == 1
            and items[0].get("model_id") == "qwen2.5:0.5b",
            "count=%d items=%r" % (len(items), items),
        )

        # ── P2 补充：Ollama 不在线**不算错误** ──
        with mock.patch("urllib.request.urlopen",
                        side_effect=OSError("connection refused")):
            down = client.get("/api/ollama/tags").json()
        _record(
            "P2 Ollama 离线 ⇒ online=False 且不报错",
            down.get("ok") is True and down.get("online") is False
            and bool(down.get("error")),
            "online=%r error=%r" % (down.get("online"), down.get("error")),
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
        "===== 阶段十三 P2 · Agent 免命令行 Ollama 模型管理（沙箱）：%d/%d 通过 ====="
        % (_PASS, _PASS + _FAIL)
    )
    return 0 if _FAIL == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
