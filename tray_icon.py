"""
tray_icon.py — 系统托盘图标

菜单项：
1. 当前状态（挂机中 / 已让退 / 已暂停 / 推理进行中）
2. 显存占用（已用 / 总量）
3. 显卡温度
4. 一键暂停 / 恢复
5. 退出

实现要点：
- 图标用 Pillow 运行时绘制，不依赖二进制资源文件（便于开源分发）
- 每 2 秒刷新一次菜单与图标颜色，颜色随节点状态变化
- pystray / Pillow 缺失时降级为"无托盘"模式，不影响主服务运行
"""

from __future__ import annotations

import threading
import time
from typing import Optional

from agent_config import get_config
from console import enable_utf8_output
from telemetry import collect_gpu_metrics
from yield_monitor import NodeState, get_yield_monitor

enable_utf8_output()

try:
    import pystray
    from PIL import Image, ImageDraw

    TRAY_AVAILABLE = True
except ImportError:  # pragma: no cover - 可选依赖缺失
    pystray = None  # type: ignore[assignment]
    Image = None  # type: ignore[assignment]
    ImageDraw = None  # type: ignore[assignment]
    TRAY_AVAILABLE = False


# 状态 → (图标颜色, 状态文案)
_STATE_STYLE = {
    NodeState.IDLE: ((46, 204, 113), "挂机中"),
    NodeState.BUSY: ((52, 152, 219), "推理进行中"),
    NodeState.YIELDED: ((241, 196, 15), "已让退"),
    NodeState.PAUSED: ((231, 76, 60), "已暂停"),
}

REFRESH_INTERVAL_SEC = 2.0


def _make_icon(color: tuple, size: int = 64) -> "Image.Image":
    """绘制一个实心圆点作为托盘图标。"""
    image = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    draw = ImageDraw.Draw(image)
    margin = max(2, size // 8)
    draw.ellipse(
        [margin, margin, size - margin, size - margin],
        fill=(*color, 255),
        outline=(255, 255, 255, 230),
        width=max(1, size // 32),
    )
    return image


class TrayIcon:
    """APIPaw 托盘控制器。"""

    def __init__(self) -> None:
        self._cfg = get_config()
        self._monitor = get_yield_monitor()
        self._icon = None
        self._thread: Optional[threading.Thread] = None
        self._stop_event = threading.Event()
        self._last_gpu: dict = {}

    # ───────────────────────── 菜单构造 ─────────────────────────

    def _build_menu(self, pystray_module):
        menu = pystray_module.Menu(
            pystray_module.MenuItem(
                f"状态：{self._monitor.state_label}",
                None,
                enabled=False,
            ),
            pystray_module.MenuItem(
                f"显存占用：{self._vram_text()}",
                None,
                enabled=False,
            ),
            pystray_module.MenuItem(
                f"显卡温度：{self._temp_text()}",
                None,
                enabled=False,
            ),
            pystray_module.Menu.SEPARATOR,
            pystray_module.MenuItem(
                "暂停节点" if not self._monitor.is_paused else "恢复节点",
                self._toggle_pause,
            ),
            pystray_module.Menu.SEPARATOR,
            pystray_module.MenuItem("退出", self._on_quit),
        )
        return menu

    def _vram_text(self) -> str:
        used = self._last_gpu.get("vram_used_gb")
        total = self._last_gpu.get("vram_total_gb")
        if used is None or total is None:
            return "未知"
        return f"{used:.1f} GB / {total:.1f} GB"

    def _temp_text(self) -> str:
        temp = self._last_gpu.get("gpu_temp_c")
        if temp is None:
            return "未知"
        return f"{temp:.0f} °C"

    # ───────────────────────── 交互回调 ─────────────────────────

    def _toggle_pause(self, icon=None, item=None) -> None:
        if self._monitor.is_paused:
            self._monitor.resume()
        else:
            self._monitor.pause()
        self._refresh(icon)

    def _on_quit(self, icon=None, item=None) -> None:
        print("[tray_icon] 收到退出指令")
        self._stop_event.set()
        if icon is not None:
            icon.stop()

    # ───────────────────────── 刷新循环 ─────────────────────────

    def _refresh(self, icon=None) -> None:
        """刷新菜单文案与图标颜色。"""
        if icon is None:
            return
        try:
            self._last_gpu = collect_gpu_metrics()
            color, _label = _STATE_STYLE.get(self._monitor.state, ((149, 165, 166), "未知"))
            icon.icon = _make_icon(color)
            icon.menu = self._build_menu(pystray)
            icon.update_menu()
        except Exception as exc:  # noqa: BLE001 - 托盘异常不能影响主服务
            print(f"[tray_icon] 刷新失败: {exc}")

    def _refresh_loop(self, icon) -> None:
        while not self._stop_event.is_set():
            self._refresh(icon)
            self._stop_event.wait(REFRESH_INTERVAL_SEC)

    # ───────────────────────── 生命周期 ─────────────────────────

    def run(self) -> bool:
        """
        在**当前线程**运行托盘消息循环（Windows 需主线程）。

        返回 False 表示托盘不可用（缺少 pystray/Pillow）。
        """
        if not TRAY_AVAILABLE:
            print("[tray_icon] 未安装 pystray/Pillow，跳过托盘图标")
            return False
        try:
            color, _ = _STATE_STYLE.get(self._monitor.state, ((149, 165, 166), "未知"))
            self._icon = pystray.Icon(
                "apipaw-agent",
                icon=_make_icon(color),
                title=f"APIPaw Agent · {self._monitor.state_label}",
                menu=self._build_menu(pystray),
            )
            threading.Thread(
                target=self._refresh_loop,
                args=(self._icon,),
                name="TrayRefreshThread",
                daemon=True,
            ).start()
            self._icon.run()
            return True
        except Exception as exc:  # noqa: BLE001
            print(f"[tray_icon] 托盘启动失败: {exc}")
            return False

    def stop(self) -> None:
        """停止托盘（可在其它线程调用）。"""
        self._stop_event.set()
        if self._icon is not None:
            try:
                self._icon.stop()
            except Exception:  # pragma: no cover
                pass


_tray_instance: Optional[TrayIcon] = None


def start_tray() -> Optional[TrayIcon]:
    """创建（并返回）托盘实例；调用方需在合适线程执行 ``run()``。"""
    global _tray_instance
    if _tray_instance is None:
        _tray_instance = TrayIcon()
    return _tray_instance


def stop_tray() -> None:
    """停止托盘。"""
    if _tray_instance is not None:
        _tray_instance.stop()


__all__ = ["TrayIcon", "start_tray", "stop_tray", "TRAY_AVAILABLE"]
