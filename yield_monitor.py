"""
yield_monitor.py — APIPaw 极速让退监视器（50ms SLA）

目标
----
用户一旦触碰键鼠（外设活动），节点必须在 **50ms 内** 完成让退：
状态切到 ``YIELDED`` 并释放显存，避免抢占用户前台体验。

技术要点
--------
1. ``GetLastInputInfo`` + ``GetTickCount64`` 读取系统级最后输入时间（覆盖键鼠等全部外设）
2. ``timeBeginPeriod(1)`` 把 Windows 计时器精度从 ~15.6ms 提到 1ms，
   这是 50ms SLA 能够稳定成立的前提（默认精度下单次轮询抖动就可能 >15ms）
3. 轮询间隔默认 5ms，独立守护线程运行
4. **先切状态、后释放显存**：``torch.cuda.empty_cache()`` 可能耗时上百毫秒，
   若同步执行会直接击穿 50ms 预算；因此状态切换先行，显存释解放到独立线程
5. 显存释放失败不影响状态机，异常被吞掉并记录（不让监控线程死掉）

跨平台
------
Windows 走 Win32 原生实现；macOS 走 ``ioreg -c IOHIDSystem`` 的
``HIDIdleTime`` 原生实现（阶段二十 P1 · 苹果芯片原生适配），
Linux 提供等价降级实现（无输入事件源，保持 IDLE），
保证本模块在任意平台都能 import，不阻断安装与文档构建。
"""

from __future__ import annotations

import ctypes
import gc
import os
import platform
import re
import subprocess
import sys
import threading
import time
from enum import Enum, auto
from typing import Callable, Dict, Optional

from agent_config import get_config
from console import enable_utf8_output

enable_utf8_output()

IS_WINDOWS = sys.platform == "win32"
IS_MACOS = sys.platform == "darwin"

# 让退目标：外设活动到状态切换的延迟上限（毫秒）
DEFAULT_YIELD_SLA_MS = 50

# 判定"用户正在操作"的窗口：最后一次输入距今小于该值即视为活动
# 取 1000ms 是为了容忍 GetLastInputInfo 的时间戳粒度（系统 tick）
ACTIVITY_WINDOW_MS = 1000


# ──────────────────────────────────────────────────────────────
# Windows API 绑定
# ──────────────────────────────────────────────────────────────
if IS_WINDOWS:
    user32 = ctypes.windll.user32
    kernel32 = ctypes.windll.kernel32

    class LASTINPUTINFO(ctypes.Structure):
        """``typedef struct tagLASTINPUTINFO { UINT cbSize; DWORD dwTime; }``"""

        _fields_ = [
            ("cbSize", ctypes.c_uint),
            ("dwTime", ctypes.c_uint32),
        ]

    _user32 = user32
    _kernel32 = kernel32
else:  # pragma: no cover - 非 Windows 平台
    _user32 = None
    _kernel32 = None


# ──────────────────────────────────────────────────────────────
# macOS 原生空闲探测（阶段二十 P1 · 苹果芯片原生适配）
# ──────────────────────────────────────────────────────────────
#
# Windows 用 ``GetLastInputInfo``（内核 API，亚毫秒级），macOS 没有等价 API，
# 但 ``ioreg -c IOHIDSystem`` 会吐出 ``"HIDIdleTime" = <纳秒>`` ——
# 那是系统级的键鼠空闲时长（覆盖全部外设），语义与 GetLastInputInfo 等价。
#
# ⚠️ **性能**：`ioreg` 是一次子进程调用（约 5~20ms）。若在 5ms 的轮询里
#    同步调用它，轮询节拍会被拖垮，50ms 让退 SLA 直接失守。
#    ⇒ 这里用一个**后台刷新线程**持续采样，轮询线程只读缓存值：
#    检测延迟上界 = 采样间隔（默认 30ms）+ 轮询节拍，仍在 50ms 预算内。

# HIDIdleTime 的正则（形如 ``"HIDIdleTime" = 1234567890``）
_MACOS_HID_IDLE_RE = re.compile(r'"HIDIdleTime"\s*=\s*(\d+)')

# 后台采样间隔（秒）—— 30ms ⇒ 检测延迟上界 < 50ms SLA
MACOS_IDLE_REFRESH_SEC = 0.03


def _parse_macos_idle_seconds(text: str) -> Optional[float]:
    """从 ``ioreg`` 原始输出里解析 ``HIDIdleTime``（**纳秒 ⇒ 秒**）。

    **纯函数**（不碰子进程）⇒ 可在任意平台上单测解析口径。

    ``ioreg -c IOHIDSystem`` 的输出形如::

        "HIDIdleTime" = 1234567890

    解析不到 / 数字非法 ⇒ 返回 ``None``（= 未知，调用方保持现状）。
    """
    if not text:
        return None
    match = _MACOS_HID_IDLE_RE.search(text)
    if not match:
        return None
    try:
        nanoseconds = int(match.group(1))
    except (TypeError, ValueError):
        return None
    return round(nanoseconds / 1_000_000_000.0, 3)


def _get_macos_idle_seconds() -> Optional[float]:
    """读取 macOS 系统级键鼠空闲时长（秒）。

    实现：``ioreg -c IOHIDSystem`` 输出里的 ``"HIDIdleTime"``（**纳秒**）⇒ 秒。

    ⚠️ 命令不可用 / 输出里没有该字段 / 解析失败 ⇒ 返回 ``None``
      （调用方据此"保持当前状态"，绝不把异常当成"用户正在操作"）。

    本函数是**同步阻塞**的（一次子进程调用）—— 生产路径上由
    :class:`_MacosIdleProbe` 的后台线程调用，轮询线程只读缓存。
    """
    if not IS_MACOS:
        return None
    try:
        proc = subprocess.run(
            ["ioreg", "-c", "IOHIDSystem"],
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            timeout=2.0,
        )
    except Exception as exc:  # noqa: BLE001 - 命令不可用时安全回退
        print("[yield_monitor] ioreg 空闲探测失败: %s" % exc)
        return None
    try:
        text = proc.stdout.decode("utf-8", "replace")
    except Exception:  # pragma: no cover - 解码失败等价于探测失败
        return None
    return _parse_macos_idle_seconds(text)


class _MacosIdleProbe:
    """macOS 空闲探测的后台采样器（缓存 + 刷新线程）。"""

    def __init__(self, interval_sec: float = MACOS_IDLE_REFRESH_SEC) -> None:
        self._interval = max(0.005, float(interval_sec or MACOS_IDLE_REFRESH_SEC))
        self._lock = threading.Lock()
        self._value: Optional[float] = None
        self._at: float = 0.0
        self._thread: Optional[threading.Thread] = None
        self._stop = threading.Event()

    def refresh(self) -> Optional[float]:
        """同步采样一次并更新缓存。"""
        value = _get_macos_idle_seconds()
        with self._lock:
            self._value = value
            self._at = time.time()
        return value

    def read(self) -> Optional[float]:
        """读缓存值；缓存为空或过期则同步补采一次。"""
        with self._lock:
            value = self._value
            at = self._at
        if value is None or (time.time() - at) > (self._interval * 4):
            return self.refresh()
        return value

    def _loop(self) -> None:
        while not self._stop.is_set():
            try:
                self.refresh()
            except Exception:  # noqa: BLE001 - 采样线程不允许退出
                pass
            self._stop.wait(self._interval)

    def start(self) -> None:
        if self._thread is not None and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._loop,
                                        name="MacosIdleProbe", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        self._thread = None


_macos_idle_probe = _MacosIdleProbe()


class NodeState(Enum):
    """节点运行状态。"""

    IDLE = auto()     # 挂机中（可接单）
    BUSY = auto()     # 推理进行中
    YIELDED = auto()  # 已让退（用户正在使用电脑）
    PAUSED = auto()   # 已暂停（用户手动暂停）


STATE_LABELS: Dict[NodeState, str] = {
    NodeState.IDLE: "挂机中",
    NodeState.BUSY: "推理进行中",
    NodeState.YIELDED: "已让退",
    NodeState.PAUSED: "已暂停",
}


def is_force_idle_enabled() -> bool:
    """强制把节点钉在 IDLE：**环境变量 ``APIPAW_FORCE_IDLE=1`` 或配置项
    ``force_idle=true``**（二者任一为真即为真）。

    某些环境（控制台程序持续注入输入事件，例如前台运行的 Claude Code TUI）
    会让 ``GetLastInputInfo`` 的空闲时间恒为 0，节点将永远无法进入 IDLE，
    云端也就无法派单。

    阶段七 P2：此前**只有环境变量** ⇒ 想开挂机模式必须手工改 ``start.bat``。
    现在配置项 ``force_idle`` 可持久化、面板可勾选、**热生效**（每次判定都
    重读配置单例），不用重启、不用改脚本。

    ⚠️ 判定的读取路径必须是**容错**的：配置文件损坏 / pydantic 缺失时
    一律回落到"不强制"，绝不因配置问题让节点起不来。
    """
    if os.getenv("APIPAW_FORCE_IDLE", "").strip().lower() in ("1", "true", "yes", "on"):
        return True
    try:
        return bool(get_config().force_idle)
    except Exception:  # noqa: BLE001 - 配置异常一律回落到"不强制"
        return False


def release_gpu_memory() -> bool:
    """
    释放显存缓存。返回是否真的调用了 CUDA 释放。

    torch 是可选依赖：未安装时退化为 Python GC 回收，保证模块可独立运行。
    """
    try:
        import torch  # type: ignore

        if torch.cuda.is_available():
            torch.cuda.empty_cache()
            return True
    except ImportError:
        pass  # torch 为可选依赖，未安装时静默降级
    except Exception as exc:  # noqa: BLE001 - 释放失败不应影响主流程
        print(f"[yield_monitor] CUDA 显存释放失败: {exc}")
    try:
        gc.collect()
    except Exception:  # pragma: no cover
        pass
    return False


class YieldMonitor:
    """
    让退监视器：轮询系统输入状态，驱动节点在 IDLE / BUSY / YIELDED / PAUSED 间切换。

    对外接口：
    - ``state`` / ``state_label`` / ``is_idle`` / ``is_busy`` / ``is_yielded`` / ``is_paused``
    - ``pause()`` / ``resume()`` / ``set_busy()`` / ``set_idle()`` / ``yield_now()``
    - ``metrics()`` 返回让退延迟等运行指标
    """

    def __init__(
        self,
        on_yield_callback: Optional[Callable[[], None]] = None,
        notify_state_callback: Optional[Callable[[NodeState], None]] = None,
    ) -> None:
        self._cfg = get_config()
        self._poll_interval_ms = max(1, int(self._cfg.poll_interval_ms))
        self._sla_ms = int(self._cfg.vram_yield_ms)

        self._state = NodeState.IDLE
        self._state_lock = threading.RLock()

        # 监控启动瞬间的空闲毫秒数：用于区分「监控期内的新输入」与「启动前的残留活动」
        self._start_idle_ms: Optional[int] = None

        self._on_yield_callback = on_yield_callback
        self._notify_state_callback = notify_state_callback

        self._thread: Optional[threading.Thread] = None
        self._release_thread: Optional[threading.Thread] = None
        self._stop_event = threading.Event()
        self._timer_precision_enabled = False

        # 运行指标
        self._yield_count = 0
        self._last_yield_time: Optional[float] = None
        self._last_yield_latency_ms: Optional[float] = None
        self._max_yield_latency_ms: Optional[float] = None
        self._sla_violations = 0
        self._started_at = time.time()

    # ───────────────────────── 状态属性 ─────────────────────────

    @property
    def state(self) -> NodeState:
        with self._state_lock:
            return self._state

    @property
    def state_label(self) -> str:
        with self._state_lock:
            return STATE_LABELS.get(self._state, "未知")

    @property
    def is_idle(self) -> bool:
        if is_force_idle_enabled():
            return True
        with self._state_lock:
            return self._state == NodeState.IDLE

    @property
    def is_busy(self) -> bool:
        with self._state_lock:
            return self._state == NodeState.BUSY

    @property
    def is_yielded(self) -> bool:
        with self._state_lock:
            return self._state == NodeState.YIELDED

    @property
    def is_paused(self) -> bool:
        with self._state_lock:
            return self._state == NodeState.PAUSED

    @property
    def yield_count(self) -> int:
        return self._yield_count

    @property
    def last_yield_latency_ms(self) -> Optional[float]:
        return self._last_yield_latency_ms

    # ───────────────────────── 控制方法 ─────────────────────────

    def pause(self) -> None:
        """手动暂停节点。"""
        with self._state_lock:
            if self._state == NodeState.PAUSED:
                return
            self._state = NodeState.PAUSED
        print("[yield_monitor] ⏸ 节点已暂停")
        self._notify(NodeState.PAUSED)

    def resume(self) -> None:
        """手动恢复节点到挂机状态。"""
        with self._state_lock:
            if self._state == NodeState.PAUSED:
                self._state = NodeState.IDLE
        print("[yield_monitor] ▶ 节点已恢复")
        self._notify(NodeState.IDLE)

    def set_busy(self) -> bool:
        """推理开始时调用；非 IDLE 状态拒绝接单。"""
        with self._state_lock:
            if self._state != NodeState.IDLE:
                return False
            self._state = NodeState.BUSY
        self._notify(NodeState.BUSY)
        return True

    def set_idle(self) -> None:
        """推理结束时回到挂机状态。"""
        with self._state_lock:
            if self._state == NodeState.BUSY:
                self._state = NodeState.IDLE
        self._notify(NodeState.IDLE)

    def yield_now(self, reason: str = "manual") -> None:
        """
        立即让退：先切状态（保证 50ms SLA），再异步释放显存。

        状态先行的原因是 ``torch.cuda.empty_cache()`` 耗时可达数百毫秒，
        同步执行会让"用户已回到电脑"与"节点仍可接单"之间出现危险窗口。
        """
        with self._state_lock:
            if self._state in (NodeState.PAUSED, NodeState.YIELDED):
                return
            self._state = NodeState.YIELDED

        self._yield_count += 1
        self._last_yield_time = time.time()
        self._last_yield_latency_ms = 0.0  # 手动让退：立即生效，延迟记 0
        self._notify(NodeState.YIELDED)
        print(f"[yield_monitor] ⚡ 已让退（{reason}），显存释放已排队")

        # 显存释放在独立线程执行，不阻塞状态机与轮询节拍
        self._schedule_memory_release()

    # ───────────────────────── 显存释放 ─────────────────────────

    def _schedule_memory_release(self) -> None:
        """把显存释放丢到独立线程，保证 50ms 让退不被 CUDA 释放耗时拖垮。"""

        def _worker() -> None:
            try:
                if self._on_yield_callback:
                    self._on_yield_callback()
                else:
                    release_gpu_memory()
            except Exception as exc:  # noqa: BLE001
                print(f"[yield_monitor] 显存释放回调异常: {exc}")
            finally:
                gc.collect()

        self._release_thread = threading.Thread(
            target=_worker, name="YieldMemoryRelease", daemon=True
        )
        self._release_thread.start()

    # ───────────────────────── Win32 探测 ─────────────────────────

    def _get_idle_milliseconds(self) -> Optional[int]:
        """
        返回系统级空闲毫秒数；无法探测的平台返回 ``None``。

        - **Windows**：``GetLastInputInfo`` + ``GetTickCount64()``
          （64 位，避免 ``GetTickCount()`` 49.7 天回绕）。
        - **macOS（阶段二十 P1）**：``ioreg -c IOHIDSystem`` 的 ``HIDIdleTime``，
          经后台采样器读缓存（不阻塞轮询节拍）。
        """
        if IS_MACOS:
            seconds = _macos_idle_probe.read()
            if seconds is None:
                return None
            return int(seconds * 1000)
        if not IS_WINDOWS or _kernel32 is None or _user32 is None:
            return None
        try:
            tick_count = _kernel32.GetTickCount64()
            lii = LASTINPUTINFO()
            lii.cbSize = ctypes.sizeof(LASTINPUTINFO)
            if not _user32.GetLastInputInfo(ctypes.byref(lii)):
                return None
            return int(tick_count - lii.dwTime)
        except Exception as exc:  # noqa: BLE001
            print(f"[yield_monitor] GetLastInputInfo 调用失败: {exc}")
            return None

    def _enable_timer_precision(self) -> bool:
        """
        把 Windows 计时器精度提升到 1ms。

        默认精度是 15.6ms，``time.sleep(0.005)`` 实际可能睡到 ~15.6ms，
        单这一项就可能吃掉 50ms 预算的三分之一。开启后轮询抖动降到 ~1ms。
        """
        if not IS_WINDOWS:
            return False
        try:
            winmm = ctypes.windll.winmm
            winmm.timeBeginPeriod(1)
            self._timer_precision_enabled = True
            return True
        except Exception as exc:  # noqa: BLE001 - 非致命
            print(f"[yield_monitor] 计时器精度提升失败（不影响功能）: {exc}")
            return False

    def _restore_timer_precision(self) -> None:
        if IS_WINDOWS and self._timer_precision_enabled:
            try:
                ctypes.windll.winmm.timeEndPeriod(1)
            except Exception:  # pragma: no cover
                pass
            self._timer_precision_enabled = False

    # ───────────────────────── 轮询主循环 ─────────────────────────

    def _poll_loop(self) -> None:
        poll_seconds = self._poll_interval_ms / 1000.0
        idle_threshold_ms = self._cfg.idle_threshold_ms

        while not self._stop_event.is_set():
            try:
                if is_force_idle_enabled():
                    # 联调模式：跳过状态机，钉住 IDLE 让心跳持续上报"可派单"
                    with self._state_lock:
                        if self._state != NodeState.IDLE:
                            self._state = NodeState.IDLE
                    self._stop_event.wait(poll_seconds)
                    continue

                idle_ms = self._get_idle_milliseconds()
                if idle_ms is None:
                    # 非 Windows 或探测失败：保持当前状态
                    self._stop_event.wait(poll_seconds)
                    continue

                with self._state_lock:
                    current = self._state

                if current == NodeState.PAUSED:
                    # 暂停状态不做任何检测
                    pass
                elif current in (NodeState.BUSY, NodeState.YIELDED):
                    # 长时间无输入 → 回到挂机状态
                    if idle_ms >= idle_threshold_ms:
                        with self._state_lock:
                            if self._state == current:
                                self._state = NodeState.IDLE
                        print("[yield_monitor] 🟢 回到挂机中")
                        self._notify(NodeState.IDLE)
                elif current == NodeState.IDLE:
                    # 挂机中检测到外设活动 → 立即让退
                    if idle_ms < ACTIVITY_WINDOW_MS:
                        latency = self._measure_latency(idle_ms)
                        with self._state_lock:
                            if self._state != NodeState.IDLE:
                                self._stop_event.wait(poll_seconds)
                                continue
                            self._state = NodeState.YIELDED
                        self._record_yield(latency)
                        print(
                            f"[yield_monitor] ⚡ 侦测到外设活动，让退延迟 {latency:.0f}ms "
                            f"(SLA {self._sla_ms}ms)"
                        )
                        self._notify(NodeState.YIELDED)
                        self._schedule_memory_release()

            except Exception as exc:  # noqa: BLE001 - 监控线程不允许退出
                print(f"[yield_monitor] 轮询异常: {exc}")

            self._stop_event.wait(poll_seconds)

    def _measure_latency(self, idle_ms: int) -> float:
        """
        计算本次让退的反应延迟（毫秒）。

        ``idle_ms`` 是「距上次输入事件的时长」，即输入发生到被本节点感知的时间差，
        天然就是让退延迟的上界。

        例外：若监控启动后**没有**发生新的输入事件（空闲计数未被重置，即
        ``idle_ms >= 启动时的空闲计数``），说明这次活动早于监控开始
        （例如用户刚敲完命令就启动了节点）。此时节点几乎是瞬时响应的，
        若把 idle_ms 当作延迟会凭空制造一次 SLA 违约，故记为 0。
        """
        if self._start_idle_ms is not None and idle_ms >= self._start_idle_ms:
            return 0.0
        return float(idle_ms)

    def _record_yield(self, latency_ms: float) -> None:
        self._yield_count += 1
        self._last_yield_time = time.time()
        self._last_yield_latency_ms = latency_ms
        if self._max_yield_latency_ms is None or latency_ms > self._max_yield_latency_ms:
            self._max_yield_latency_ms = latency_ms
        if latency_ms > self._sla_ms:
            self._sla_violations += 1
            print(
                f"[yield_monitor] ⚠ 让退延迟 {latency_ms:.0f}ms 超出 SLA {self._sla_ms}ms"
            )

    def _notify(self, state: NodeState) -> None:
        if self._notify_state_callback:
            try:
                self._notify_state_callback(state)
            except Exception as exc:  # noqa: BLE001
                print(f"[yield_monitor] 状态通知回调异常: {exc}")

    # ───────────────────────── 生命周期 ─────────────────────────

    def start(self) -> None:
        """启动让退监控守护线程。"""
        if self._thread is not None and self._thread.is_alive():
            print("[yield_monitor] 监控线程已在运行")
            return
        self._enable_timer_precision()
        self._stop_event.clear()
        # 阶段二十 P1：macOS 上启动空闲采样线程（轮询线程只读缓存）
        if IS_MACOS:
            _macos_idle_probe.start()
        # 记录启动时的空闲计数：此后空闲计数被"重置"即说明发生了新的输入事件
        self._start_idle_ms = self._get_idle_milliseconds()
        self._thread = threading.Thread(
            target=self._poll_loop, name="YieldMonitorThread", daemon=True
        )
        self._thread.start()
        print(
            f"[yield_monitor] 启动｜轮询 {self._poll_interval_ms}ms｜"
            f"让退 SLA {self._sla_ms}ms｜空闲阈值 {self._cfg.idle_threshold_seconds}s｜"
            f"平台 {platform.system()}"
        )

    def stop(self) -> None:
        """停止监控线程并恢复计时器精度。"""
        self._stop_event.set()
        if IS_MACOS:
            _macos_idle_probe.stop()
        if self._thread is not None:
            self._thread.join(timeout=2.0)
            self._thread = None
        self._restore_timer_precision()
        print("[yield_monitor] 监控线程已停止")

    # ───────────────────────── 运行指标 ─────────────────────────

    def metrics(self) -> Dict[str, object]:
        """返回让退相关运行指标（供 /health 与心跳上报使用）。"""
        return {
            "state": self.state.name,
            "state_label": self.state_label,
            "yield_count": self._yield_count,
            "last_yield_latency_ms": self._last_yield_latency_ms,
            "max_yield_latency_ms": self._max_yield_latency_ms,
            "sla_ms": self._sla_ms,
            "sla_violations": self._sla_violations,
            "poll_interval_ms": self._poll_interval_ms,
            "timer_precision_1ms": self._timer_precision_enabled,
            # 阶段二十 P1：空闲探测来源（win32 / macos_ioreg / none）
            "idle_probe": ("macos_ioreg" if IS_MACOS
                           else ("win32" if IS_WINDOWS else "none")),
            "uptime_seconds": round(time.time() - self._started_at, 1),
        }


# ──────────────────────────────────────────────────────────────
# 全局单例
# ──────────────────────────────────────────────────────────────
_monitor_instance: Optional[YieldMonitor] = None
_monitor_lock = threading.Lock()


def get_yield_monitor(
    on_yield_callback: Optional[Callable[[], None]] = None,
    notify_state_callback: Optional[Callable[[NodeState], None]] = None,
) -> YieldMonitor:
    """获取全局让退监视器单例。"""
    global _monitor_instance
    if _monitor_instance is None:
        with _monitor_lock:
            if _monitor_instance is None:
                _monitor_instance = YieldMonitor(
                    on_yield_callback=on_yield_callback,
                    notify_state_callback=notify_state_callback,
                )
    return _monitor_instance


__all__ = [
    "NodeState",
    "YieldMonitor",
    "STATE_LABELS",
    "get_yield_monitor",
    "is_force_idle_enabled",
    "release_gpu_memory",
    "_get_macos_idle_seconds",
    "_parse_macos_idle_seconds",
    "IS_MACOS",
]
