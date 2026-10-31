"""
console.py — 控制台编码兜底

中文 Windows 的默认代码页是 GBK，``print("⚡ 已让退")`` 会抛
``UnicodeEncodeError``，从而打断正在运行的让退轮询线程——这是致命的，
因为一次日志异常就会让节点失去让退能力。

本模块把标准输出/错误流改为 ``errors="replace"``：
中文照常显示，不可编码的符号（如 emoji）替换为 "?"，保证进程不会因日志而崩溃。
若运行环境已是 UTF-8（如 ``chcp 65001`` 或 WSL），该设置无副作用。
"""

from __future__ import annotations

import sys


def enable_utf8_output() -> None:
    """让 stdout/stderr 具备编码兜底能力（幂等，可重复调用）。"""
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure is None:
            continue
        try:
            reconfigure(errors="replace")
        except Exception:  # pragma: no cover - 流不可重配时忽略
            pass


enable_utf8_output()

__all__ = ["enable_utf8_output"]
