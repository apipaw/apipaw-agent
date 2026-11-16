"""
test_phase8_p0_config_isolation.py —— 阶段八 P0 · AgentConfig 单例污染根治自检

背景
----
阶段七 P1-P3 验收时踩到一个**静默污染**的坑：

``AgentConfig`` 是单例，而 ``__new__`` **无视构造参数**、永远返回同一个实例。
于是 ``AgentConfig(config_path=<临时文件>)`` 会被**悄悄吞掉**，
``cfg.update(...)`` 直接把内容写进**用户真实的** ``agent.config.json``。

实测后果：跑一次单测就把真机配置的 ``force_idle`` / ``backend_model`` 冲掉了，
**全程没有任何报错**（这才是它危险的地方）。

阶段八 P0 的契约（``agent_config.py``）：

- ``AgentConfig()`` / ``get_config()`` ⇒ 单例，路径 = 仓库内
  ``agent.config.json``（与旧版行为一致）；
- ``AgentConfig(config_path=X)`` ⇒ **独立实例**，只读写 ``X``，
  与单例互不影响，**绝不会**碰真机配置；
- ``AgentConfig.reset_instance()`` ⇒ 测试用，丢弃单例。

本文件锁死四条不变量：

- **I1** 显式传 ``config_path`` 拿到的是**独立实例**（不是单例）；
- **I2** 该实例的读写**只**落在它自己的路径上；
- **I3** 跑完测试后，真机 ``agent.config.json`` 的 **mtime 与内容分毫不动**
  （测试自带"保险丝"：万一回归了也会把字节与 mtime 还原，绝不留下污染）；
- **I4** 单例语义没被破坏：``AgentConfig() is get_config()``，
  且 ``reset_instance()`` 能真正丢弃单例。
"""

from __future__ import annotations

import hashlib
import os
import sys
import tempfile
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[0]
sys.path.insert(0, str(REPO_ROOT))

import agent_config  # noqa: E402

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


def _snapshot(path: Path):
    """快照：内容摘要 + mtime（纳秒）+ 大小。"""
    if not path.exists():
        return (None, None, None)
    raw = path.read_bytes()
    return (
        hashlib.sha256(raw).hexdigest(),
        path.stat().st_mtime_ns,
        len(raw),
    )


def _restore(path: Path, raw: bytes, mtime_ns: int) -> None:
    """保险丝：把文件**字节与 mtime** 一起还原（回归时也不留污染）。"""
    if not path.exists() and raw is None:
        return
    if raw is None:
        try:
            path.unlink()
        except OSError:
            pass
        return
    path.write_bytes(raw)
    os.utime(str(path), ns=(mtime_ns, mtime_ns))


def main() -> int:
    real_path = agent_config.PROJECT_ROOT / agent_config.CONFIG_FILE_NAME
    real_bytes = real_path.read_bytes() if real_path.exists() else None
    before = _snapshot(real_path)
    print("[info] 真机配置：%s" % real_path)

    tmp_dir = tempfile.mkdtemp(prefix="apipaw_p8_p0_")
    tmp_path = Path(tmp_dir) / "agent.config.json"

    try:
        singleton = agent_config.get_config()

        # ── I1：显式 config_path ⇒ 独立实例 ──
        isolated = agent_config.AgentConfig(config_path=tmp_path)
        _record(
            "I1 显式 config_path 返回独立实例（不再是单例）",
            isolated is not singleton and getattr(isolated, "_isolated", False) is True,
            "isolated is singleton? %r" % (isolated is singleton,),
        )
        _record(
            "I1 独立实例的路径 = 传入路径",
            Path(isolated._config_path) == tmp_path,
            "path=%s" % isolated._config_path,
        )

        # ── I2：写入只落在自己的文件上 ──
        isolated.update(node_name="phase8-isolated-node", force_idle=True)
        saved = tmp_path.read_text(encoding="utf-8")
        _record(
            "I2 独立实例的写入只落在自己的文件",
            "phase8-isolated-node" in saved and '"force_idle": true' in saved,
            "临时文件已写入 node_name/force_idle",
        )

        # ── I3：真机配置分毫不动 ──
        after = _snapshot(real_path)
        _record(
            "I3 真机 agent.config.json 内容分毫不动",
            before[0] == after[0] and before[2] == after[2],
            "sha256 %s → %s" % (str(before[0])[:12], str(after[0])[:12]),
        )
        _record(
            "I3 真机 agent.config.json mtime 分毫不动",
            before[1] == after[1],
            "mtime_ns %s → %s" % (before[1], after[1]),
        )

        # ── I4：单例语义未被破坏 ──
        _record(
            "I4 AgentConfig() 与 get_config() 仍是同一单例",
            agent_config.AgentConfig() is singleton,
            "singleton=%r" % (singleton is not None,),
        )
        agent_config.AgentConfig.reset_instance()
        fresh = agent_config.get_config()
        _record(
            "I4 reset_instance() 能真正丢弃单例",
            fresh is not singleton
            and Path(fresh._config_path) == real_path,
            "新单例路径=%s" % fresh._config_path,
        )
    finally:
        # 保险丝：无论成败，真机配置的**字节与 mtime** 都还原
        if real_bytes is not None:
            _restore(real_path, real_bytes, before[1])

    print("")
    print(
        "===== 阶段八 P0 · AgentConfig 单例隔离（沙箱）：%d/%d 通过 ====="
        % (_PASS, _PASS + _FAIL)
    )
    return 0 if _FAIL == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
