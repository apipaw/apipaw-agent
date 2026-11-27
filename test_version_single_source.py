# -*- coding: utf-8 -*-
"""阶段二十六 P1 · Agent 版本**单一真源**回归（R-10）。

背景
----
审计发现同一份 Agent 代码里同时存在三套版本号：
    · ``agent_config.AGENT_VERSION``  = 1.0.20（真源）
    · ``agent.config.json``（本地运行期配置，被 .gitignore）= 1.0.11
    · ``README.md`` / ``PAWMODEL.md`` 的示例 = 1.0.4
而云端 UA（``cloud_http.py``）与 Mesh 对端 UA（``mesh_cluster.py``）又把版本号
**硬编码**在字符串里 —— 于是「发出去的版本」与「代码里的版本」没有任何绑定关系。

本用例把这件事变成**机器可判定的不变量**：

  A. 语义化格式合法；
  B. 云端 UA / 客户端头 / Mesh 对端 UA 三处的版本号 **== 真源**；
  C. 代码里**不再出现硬编码**的 ``APIPawAgent/1.x.y`` 字面量；
  D. CI 不再硬编码 ``--version 1.x.y``；
  E. README / PAWMODEL 的示例版本 == 真源（发版时必须同步改文档）；
  F. 本地运行期配置 ``agent.config.json``（若存在）== 真源。

跑法：``python test_version_single_source.py``
"""

import io
import os
import re
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
if HERE not in sys.path:
    sys.path.insert(0, HERE)

RESULTS = []

# Windows 控制台默认 GBK —— 用例名含非 GBK 字符会 UnicodeEncodeError。
try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")
except Exception:  # noqa: BLE001
    pass


def _record(name, ok, detail=""):
    RESULTS.append((name, ok, detail))
    print(("[PASS] " if ok else "[FAIL] ") + name + (" | " + detail if detail else ""))


def _read(name):
    path = os.path.join(HERE, name)
    if not os.path.isfile(path):
        return ""
    with io.open(path, "r", encoding="utf-8") as fh:
        return fh.read()


def main():
    from agent_config import AGENT_VERSION

    version = str(AGENT_VERSION)
    _record("A1. AGENT_VERSION 是合法语义化版本",
            bool(re.match(r"^\d+\.\d+\.\d+$", version)), "实际 %r" % version)

    # ---- B. 各处 UA 的版本号必须等于真源 ----
    import cloud_http
    import mesh_cluster

    ua = str(cloud_http.CLOUD_USER_AGENT)
    _record("B1. 云端 UA 带真源版本号",
            ua.endswith("APIPawAgent/%s" % version), "实际 %r" % ua[-40:])
    _record("B2. 客户端标识头带真源版本号",
            str(cloud_http.CLIENT_HEADER_VALUE) == "APIPawAgent/%s" % version,
            "实际 %r" % cloud_http.CLIENT_HEADER_VALUE)
    peer_ua = str(mesh_cluster._PEER_USER_AGENT)
    _record("B3. Mesh 对端 UA 带真源版本号",
            peer_ua.endswith("APIPawAgent/%s" % version), "实际 %r" % peer_ua[-40:])

    # ---- C. 代码里不得再硬编码版本字面量 ----
    hardcoded = re.compile(r"APIPawAgent/\d+\.\d+\.\d+")
    for name in ("cloud_http.py", "mesh_cluster.py"):
        hits = hardcoded.findall(_read(name))
        _record("C. %s 内无硬编码版本字面量" % name, not hits,
                "命中 %s" % hits if hits else "")

    # ---- D. CI 不得硬编码 --version ----
    ci = _read(os.path.join(".github", "workflows", "ci.yml"))
    ci_code = "\n".join(line for line in ci.splitlines()
                        if not line.strip().startswith("#"))
    ci_hits = re.findall(r"--version\s+\d+\.\d+\.\d+", ci_code)
    _record("D1. CI 未硬编码 --version 1.x.y", not ci_hits,
            "命中 %s" % ci_hits if ci_hits else "")
    _record("D2. CI 存在（可校验）", bool(ci), "长度 %d" % len(ci))

    # ---- E. 文档示例版本必须同步 ----
    readme = _read("README.md")
    pawmodel = _read("PAWMODEL.md")
    _record("E1. README 示例版本 == 真源",
            '"agent_version": "%s"' % version in readme,
            "找到 %s" % version if '"agent_version": "%s"' % version in readme
            else "未找到 %s" % version)
    _record("E2. README 不再残留旧版本 1.0.4",
            "1.0.4" not in readme, "命中" if "1.0.4" in readme else "")
    _record("E3. PAWMODEL 客户端版本 == 真源",
            ("客户端版本：`%s`" % version) in pawmodel,
            "找到 %s" % version if ("客户端版本：`%s`" % version) in pawmodel
            else "未找到 %s" % version)
    _record("E4. PAWMODEL 不再残留旧版本 1.0.4",
            "1.0.4" not in pawmodel, "命中" if "1.0.4" in pawmodel else "")

    # ---- F. 本地运行期配置（若存在）== 真源 ----
    cfg = _read("agent.config.json")
    if cfg:
        _record("F1. 本地 agent.config.json 版本 == 真源",
                '"agent_version": "%s"' % version in cfg,
                "找到 %s" % version if '"agent_version": "%s"' % version in cfg
                else "未找到 %s" % version)
    else:
        print("[--] 本地无 agent.config.json（CI / 全新克隆场景），跳过 F 组")

    failed = [r for r in RESULTS if not r[1]]
    print("")
    print("== 阶段二十六 P1 · Agent 版本单一真源自检：%d/%d 通过 ==" %
          (len(RESULTS) - len(failed), len(RESULTS)))
    if failed:
        for name, _ok, detail in failed:
            print("  [FAIL] %s | %s" % (name, detail))
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
