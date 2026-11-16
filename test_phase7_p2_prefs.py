"""
test_phase7_p2_prefs.py —— 阶段七 P2「Agent 强制待命 + 本地模型配置」回归自检

背景
----
阶段七 P2 要消灭两个手工痛点：

1. **强制待命模式**此前只能靠环境变量 ``APIPAW_FORCE_IDLE=1``，
   想开挂机模式必须**改 start.bat** ⇒ 面板上改不了。
   现在它是 ``agent.config.json`` 里的持久化配置项 ``force_idle``，
   面板勾选即生效（``yield_monitor`` 每次判定都重读配置）。

2. **本地推理模型（backend_model）**此前只能手改 ``agent.config.json``，
   现在面板有输入框 ⇒ 新增 ``POST /api/prefs``，保存即生效。

本文件锁死六条不变量：

- **P1** ``force_idle`` 是合法配置项，默认 ``False``，可持久化到磁盘；
- **P2** ``POST /api/prefs`` 能同时保存 ``force_idle`` 与 ``backend_model``，
  且**立即回读**到配置单例（"热生效"，不必重启）；
- **P3** ``yield_monitor.is_force_idle_enabled()`` 在配置为真时返回真；
- **P4** 环境变量 ``APIPAW_FORCE_IDLE=1`` 的老口径**仍然生效**（不破坏兼容）；
- **P5** ``/health`` 暴露 ``force_idle`` / ``backend_model``（面板要预填）；
- **P6** 心跳报文体带 ``models`` / ``backend_model`` / ``inference_backend``
  （阶段七 P1：节点主动告诉云端自己有哪些模型）。

⚠️ 全程使用**临时目录**里的配置文件，绝不碰用户真实的 ``agent.config.json``。
"""

from __future__ import annotations

import json
import os
import sys
import tempfile
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[0]
sys.path.insert(0, str(REPO_ROOT))

import agent_config  # noqa: E402
import main as agent_main  # noqa: E402
import telemetry  # noqa: E402
import yield_monitor  # noqa: E402

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


def _make_config(tmp_dir: str):
    """造一份指向临时文件的配置实例（**不碰用户真实的 agent.config.json**）。

    ⚠️ ``AgentConfig`` 是**单例**（``__new__`` 里带 ``_initialized`` 闸门），
    直接 ``AgentConfig(config_path=...)`` 会被单例吞掉、仍然指向仓库里的
    真实配置文件 —— 那会**改写用户的线上配置**。这里绕过 ``__new__`` 造一个
    独立实例，再把模块级单例指针指过去。
    """
    path = Path(tmp_dir) / "agent.config.json"
    cfg = object.__new__(agent_config.AgentConfig)
    agent_config.AgentConfig.__init__(cfg, config_path=path)
    return cfg, path


def main() -> int:
    tmp_dir = tempfile.mkdtemp(prefix="apipaw_p7_p2_")
    cfg, cfg_path = _make_config(tmp_dir)

    # 让所有模块都读这份临时配置
    agent_config._config_instance = cfg  # type: ignore[attr-defined]
    agent_main.get_config = lambda: cfg  # type: ignore[assignment]
    yield_monitor.get_config = lambda: cfg  # type: ignore[assignment]
    telemetry.get_config = lambda: cfg  # type: ignore[assignment]
    os.environ.pop("APIPAW_FORCE_IDLE", None)

    # ── P1：force_idle 是合法配置项，默认 False，可持久化 ──
    _record(
        "P1 force_idle 默认关闭",
        cfg.force_idle is False,
        "force_idle=%r" % (cfg.force_idle,),
    )
    cfg.update(force_idle=True, backend_model="qwen2.5:0.5b")
    saved = json.loads(cfg_path.read_text(encoding="utf-8"))
    _record(
        "P1 force_idle / backend_model 落盘",
        saved.get("force_idle") is True and saved.get("backend_model") == "qwen2.5:0.5b",
        "saved.force_idle=%r saved.backend_model=%r"
        % (saved.get("force_idle"), saved.get("backend_model")),
    )

    # ── P3：配置为真 ⇒ 强制待命判定为真 ──
    _record(
        "P3 配置项 force_idle=true ⇒ 强制待命",
        yield_monitor.is_force_idle_enabled() is True,
        "is_force_idle_enabled=%r" % (yield_monitor.is_force_idle_enabled(),),
    )

    # ── P4：环境变量老口径仍然生效 ──
    cfg.update(force_idle=False)
    os.environ["APIPAW_FORCE_IDLE"] = "1"
    _record(
        "P4 环境变量 APIPAW_FORCE_IDLE=1 仍然生效",
        yield_monitor.is_force_idle_enabled() is True,
        "env 优先于配置项",
    )
    os.environ.pop("APIPAW_FORCE_IDLE", None)
    _record(
        "P4 取消环境变量后回到配置项判定",
        yield_monitor.is_force_idle_enabled() is False,
        "配置项为 False ⇒ 不强制",
    )

    # ── P2 / P5：HTTP 层 ──
    from starlette.testclient import TestClient  # noqa: E402

    app = agent_main.create_app()
    # ⚠️ **不进 lifespan**（不进 ``with``）：lifespan 会启动 WS 隧道 + 遥测
    #    ⇒ 测试会真的连上生产云端并上报心跳。这里只测纯 HTTP 端点。
    client = TestClient(app)
    resp = client.post(
        "/api/prefs",
        json={"force_idle": True, "backend_model": "qwen2.5:0.5b"},
    )
    body = resp.json()
    _record(
        "P2 POST /api/prefs 保存运行偏好",
        resp.status_code == 200 and bool(body.get("ok")),
        "HTTP %s body=%s" % (resp.status_code, body),
    )
    _record(
        "P2 保存后立即回读生效（热生效）",
        cfg.force_idle is True and cfg.backend_model == "qwen2.5:0.5b",
        "force_idle=%r backend_model=%r" % (cfg.force_idle, cfg.backend_model),
    )
    _record(
        "P2 空偏好返回 400",
        client.post("/api/prefs", json={}).status_code == 400,
        "空更新被拒",
    )

    status = client.get("/api/status").json()
    _record(
        "P5 /api/status 暴露 force_idle / backend_model（面板预填）",
        status.get("force_idle") is True
        and status.get("backend_model") == "qwen2.5:0.5b",
        "force_idle=%r backend_model=%r"
        % (status.get("force_idle"), status.get("backend_model")),
    )

    # ── P6：心跳报文体带模型信息 ──
    reporter = telemetry.TelemetryReporter()
    payload = reporter.build_payload()
    _record(
        "P6 心跳报文带 models / backend_model / inference_backend",
        "models" in payload
        and "backend_model" in payload
        and "inference_backend" in payload
        and isinstance(payload["models"], list),
        "models=%r backend_model=%r inference_backend=%r"
        % (
            payload.get("models"),
            payload.get("backend_model"),
            payload.get("inference_backend"),
        ),
    )

    print("")
    print(
        "===== 阶段七 P2 · Agent 强制待命与本地模型配置（沙箱）：%d/%d 通过 ====="
        % (_PASS, _PASS + _FAIL)
    )
    return 0 if _FAIL == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
