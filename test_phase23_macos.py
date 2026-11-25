"""
test_phase23_macos.py —— 阶段二十三 P0 · **macOS / Apple Silicon 真机运行时**自检

为什么要有这个用例
------------------
阶段二十 P1 给 macOS 补了原生空闲探测（``ioreg -c IOHIDSystem`` 的
``HIDIdleTime``）与 Apple Silicon 统一内存折算，但**验证方式一直是
"注入 mock 跑一遍"** —— 从来没有在**真的 macOS 运行时**上跑过。
于是这些代码在 macOS 上到底能不能跑通，只有作者本机知道。

本用例在 **macOS runner 上真实执行**：
  * 真跑一次 ``ioreg``，验证 ``HIDIdleTime`` 能被解析成秒；
  * 真跑一次 ``sysctl hw.memsize``，验证统一内存能被折算成 ``vram_gb``；
  * 验证 ``yield_monitor`` 的平台分支与 ``platform.system()`` 一致。

在非 macOS 上运行时，本文件自动降级为"纯解析口径 + 注入模式"自检
（**不假装通过**：真机相关的用例在非 macOS 上标记为 SKIP 而不是 PASS）。

⚠️ 本文件**绝不污染真机 ``agent.config.json``**：结束后校验其
    字节摘要 / mtime / 长度分毫不动。
"""

from __future__ import annotations

import hashlib
import os
import platform
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[0]
sys.path.insert(0, str(REPO_ROOT))

import telemetry  # noqa: E402
import yield_monitor  # noqa: E402

_PASS = 0
_FAIL = 0
_SKIP = 0

IS_MACOS = platform.system() == "Darwin"


def _record(name: str, ok: bool, detail: str = "") -> None:
    global _PASS, _FAIL
    if ok:
        _PASS += 1
        print("[PASS] %s | %s" % (name, detail))
    else:
        _FAIL += 1
        print("[FAIL] %s | %s" % (name, detail))


def _skip(name: str, detail: str = "") -> None:
    global _SKIP
    _SKIP += 1
    print("[SKIP] %s | %s" % (name, detail))


def _snapshot(path: Path):
    if not path.exists():
        return (None, None, None)
    raw = path.read_bytes()
    return (hashlib.sha256(raw).hexdigest(), path.stat().st_mtime_ns, len(raw))


def main() -> int:
    cfg_path = REPO_ROOT / "agent.config.json"
    before = _snapshot(cfg_path)

    # ---------- 1. 解析口径（任何平台都可测）----------
    sample = (
        '+-o HIDSystem  <class IOHIDSystem, id 0x100000d2a, registered, matched, '
        'active, busy 0 (0 ms), retain 6>\n'
        '    "HIDIdleTime" = 1234567890\n'
    )
    parsed = yield_monitor._parse_macos_idle_seconds(sample)
    _record(
        "1. HIDIdleTime 纳秒 ⇒ 秒（口径：三位小数）",
        parsed is not None and abs(parsed - 1.235) < 1e-9,
        "parsed=%s" % parsed,
    )
    _record(
        "2. 空输出 ⇒ None",
        yield_monitor._parse_macos_idle_seconds("") is None,
        "ok",
    )
    _record(
        "3. 平台分支常量与 platform.system() 一致",
        yield_monitor.IS_MACOS == IS_MACOS,
        "IS_MACOS=%s system=%s" % (yield_monitor.IS_MACOS, platform.system()),
    )

    # ---------- 2. Apple Silicon 统一内存折算（注入模式，跨平台可测）----------
    os.environ["APIPAW_MOCK_APPLE_MEMORY_GB"] = "16"
    os.environ["APIPAW_MOCK_APPLE_CHIP"] = "Apple M2 Pro"
    try:
        apple = telemetry.detect_apple_silicon()
    finally:
        os.environ.pop("APIPAW_MOCK_APPLE_MEMORY_GB", None)
        os.environ.pop("APIPAW_MOCK_APPLE_CHIP", None)
    _record(
        "4. Apple Silicon 识别（注入）",
        bool(apple.get("is_apple_silicon")) and apple.get("chip") == "Apple M2 Pro",
        "chip=%s" % apple.get("chip"),
    )
    _record(
        "5. 统一内存 16GB ⇒ vram 12GB（75% 折算）",
        abs(float(apple.get("vram_gb") or 0) - 12.0) < 1e-6,
        "unified=%s vram=%s" % (apple.get("unified_memory_gb"), apple.get("vram_gb")),
    )
    _record(
        "6. Mac 上无独显 ⇒ 不谎报 GPU 温度",
        apple.get("gpu_temp_c") in (None, ""),
        "gpu_temp_c=%s" % apple.get("gpu_temp_c"),
    )

    # ---------- 3. **真机**运行时（仅 macOS 才有意义）----------
    if not IS_MACOS:
        _skip("7. 真机 ioreg 空闲探测", "当前平台 %s，非 macOS" % platform.system())
        _skip("8. 真机 sysctl 统一内存折算", "当前平台 %s，非 macOS" % platform.system())
    else:
        # 7. 真跑 ioreg
        try:
            seconds = yield_monitor._get_macos_idle_seconds()
        except Exception as exc:  # noqa: BLE001
            _record("7. 真机 ioreg 空闲探测", False, "抛异常: %s" % exc)
        else:
            if seconds is None:
                # ioreg 在无头 runner 上可能拿不到 ⇒ 不算失败，但必须说清楚
                _skip("7. 真机 ioreg 空闲探测", "ioreg 无输出（无头环境），已跳过")
            else:
                _record(
                    "7. 真机 ioreg 空闲探测",
                    isinstance(seconds, float) and seconds >= 0.0,
                    "idle=%.3fs" % seconds,
                )

        # 8. 真跑 sysctl
        real = telemetry.detect_apple_silicon()
        source = str(real.get("source") or "")
        if source == "sysctl":
            unified = float(real.get("unified_memory_gb") or 0)
            vram = float(real.get("vram_gb") or 0)
            _record(
                "8. 真机统一内存折算（75%）",
                unified > 0 and abs(vram - unified * 0.75) < 1e-3,
                "chip=%s unified=%.2f vram=%.2f" % (real.get("chip"), unified, vram),
            )
        else:
            _skip("8. 真机统一内存折算", "source=%s（sysctl 不可用）" % source)

    # ---------- 4. 真机配置零污染 ----------
    after = _snapshot(cfg_path)
    _record(
        "9. 真机 agent.config.json 分毫未动",
        before == after,
        "before=%s after=%s" % (before, after),
    )

    print("\n[test] 通过 %d / 失败 %d / 跳过 %d" % (_PASS, _FAIL, _SKIP))
    return 0 if _FAIL == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
