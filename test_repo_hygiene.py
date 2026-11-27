# -*- coding: utf-8 -*-
"""阶段二十六 P1 · 仓库卫生回归（R-09）：禁止「副本式目录」再次长出来。

背景
----
阶段六 P4 的灰度演练在仓库根留下了 ``.p4_rehearsal/`` —— 一份 **3,226 行**的
Agent 源码副本（agent_config / console / dispatch_crypto / inference_backend /
main / telemetry / token_counter / web_ui / ws_tunnel / yield_monitor）。
它已被 ``.gitignore`` 排除（所以没进版本库），但仍然：
    · 污染本地检索与 AI 辅助工具的上下文（同名模块出现两次）；
    · 让人误以为「有两份 Agent 代码在维护」；
    · 掩盖真正的仓库状态（审计时第一眼根本看不出来）。

本用例把「不许再出现副本目录」变成**机器可判定的不变量**：

  A. 仓库根（及一级子目录）里不得存在**内含同名顶层模块**的副本目录；
  B. ``.gitignore`` 仍须列出 ``.p4_rehearsal/``（万一有人删了这条，
     副本目录就会被误提交进公开仓）；
  C. 顶层模块不得在子目录里出现第二份同名 ``.py``（副本的通用形态）。

跑法：``python test_repo_hygiene.py``
"""

import io
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))

# Windows 控制台默认 GBK —— 用例名含非 GBK 字符会 UnicodeEncodeError。
try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")
except Exception:  # noqa: BLE001
    pass

RESULTS = []

#: 明确点名的历史副本目录（R-09 的具体对象）。
KNOWN_DUP_DIRS = (".p4_rehearsal",)

#: 这些目录不算「副本目录」（它们是源码，不是 Agent 模块的复制品）。
SKIP_DIRS = {".git", ".venv", "venv", "env", "__pycache__", "dist", "build",
             "node_modules", ".mypy_cache", ".pytest_cache", ".ruff_cache",
             "scripts", "docs", ".github"}


def _record(name, ok, detail=""):
    RESULTS.append((name, ok, detail))
    print(("[PASS] " if ok else "[FAIL] ") + name + (" | " + detail if detail else ""))


def _top_level_modules():
    names = []
    for entry in os.listdir(HERE):
        if entry.endswith(".py") and not entry.startswith("test_"):
            names.append(entry)
    return set(names)


def main():
    top_modules = _top_level_modules()
    _record("A0. 顶层模块清单非空", bool(top_modules), "%d 个" % len(top_modules))

    # ---- A. 不得存在含同名顶层模块的副本目录 ----
    dup_dirs = []
    for entry in sorted(os.listdir(HERE)):
        full = os.path.join(HERE, entry)
        if not os.path.isdir(full) or entry in SKIP_DIRS:
            continue
        inner = set()
        try:
            for sub in os.listdir(full):
                if sub.endswith(".py"):
                    inner.add(sub)
        except OSError:
            continue
        overlap = top_modules & inner
        if len(overlap) >= 3:  # ≥3 个同名顶层模块 ⇒ 判定为副本目录
            dup_dirs.append((entry, sorted(overlap)))

    _record("A1. 仓库内无 Agent 源码副本目录", not dup_dirs,
            "命中 %s" % dup_dirs if dup_dirs else "")

    # ---- B. .gitignore 仍须挡住历史副本目录 ----
    gi_path = os.path.join(HERE, ".gitignore")
    gi = ""
    if os.path.isfile(gi_path):
        with io.open(gi_path, "r", encoding="utf-8") as fh:
            gi = fh.read()
    for name in KNOWN_DUP_DIRS:
        _record("B. .gitignore 仍排除 %s/" % name, name + "/" in gi)

    # ---- C. 副本目录若还在本地，必须已被忽略（不进入版本库） ----
    for name in KNOWN_DUP_DIRS:
        full = os.path.join(HERE, name)
        exists = os.path.isdir(full)
        _record("C. %s/ 不存在于工作区（本地副本已清理）" % name, not exists,
                "仍存在 —— 建议直接删除该目录" if exists else "")

    # ---- D. 顶层模块不得在子目录里出现第二份同名 .py ----
    duplicates = []
    for entry in sorted(os.listdir(HERE)):
        full = os.path.join(HERE, entry)
        if not os.path.isdir(full) or entry in SKIP_DIRS:
            continue
        for sub in sorted(os.listdir(full)):
            if sub in top_modules and sub.endswith(".py"):
                duplicates.append(os.path.join(entry, sub))
    _record("D1. 顶层模块没有第二份同名副本", not duplicates,
            "命中 %s" % duplicates if duplicates else "")

    failed = [r for r in RESULTS if not r[1]]
    print("")
    print("== 阶段二十六 P1 · 仓库卫生自检：%d/%d 通过 ==" %
          (len(RESULTS) - len(failed), len(RESULTS)))
    if failed:
        for name, _ok, detail in failed:
            print("  [FAIL] %s | %s" % (name, detail))
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
